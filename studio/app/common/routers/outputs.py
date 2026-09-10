import os
from typing import Optional

import h5py
import numpy as np
import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse

from studio.app.common.core.auth.auth_dependencies import get_outputs_remote_bucket_name
from studio.app.common.core.dataview.dataview import DatasetPaths
from studio.app.common.core.dataview.thumbnail_generator import ThumbnailGenerator
from studio.app.common.core.experiment.experiment import ExptOutputPathIds
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.snakemake.smk_utils import SmkUtils
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteExperimentNotFoundError,
    RemoteExperimentSyncMode,
    RemoteStorageController,
    RemoteStorageDownloadUtils,
    RemoteStorageLockError,
    RemoteStorageReader,
    RemoteStorageSimpleWriter,
    RemoteSyncStatusFileUtil,
)
from studio.app.common.core.utils.file_reader import JsonReader, Reader
from studio.app.common.core.utils.filepath_creater import (
    create_directory,
    join_filepath,
    normalize_output_path,
)
from studio.app.common.core.utils.json_writer import JsonWriter, save_tiff2json
from studio.app.common.core.utils.path_guard import (
    secure_component,
    secure_output_relpath,
    secure_relpath,
)
from studio.app.common.core.workflow.workflow_reader import WorkflowConfigReader
from studio.app.common.core.workspace.workspace_dependencies import (
    is_workspace_available,
)
from studio.app.common.dataclass.timeseries_chunk_handler import TimeSeriesChunkHandler
from studio.app.common.schemas.outputs import JsonTimeSeriesData, OutputData
from studio.app.const import ACCEPT_FILE_EXT, ORIGINAL_DATA_EXT, ThumbnailType
from studio.app.dir_path import DIRPATH
from studio.app.optinist.routers.mat import MatGetter

router = APIRouter(prefix="/api/visualizations", tags=["visualizations"])

logger = AppLogger.get_logger()


async def get_or_generate_thumbnail(
    workspace_id: str,
    unique_id: str,
    original_path: str,
    remote_bucket_name: str,
    thumb_type: ThumbnailType,
    dataset_paths: DatasetPaths = None,
) -> str:
    """
    Get thumbnail path, generating if needed (lazy migration).

    For backward compatibility with experiments that don't have PNG thumbnails:
    1. Check if PNG thumbnail exists → return it
    2. If not, check if original file exists locally
       - If not, download from remote storage
    3. Generate PNG from the original file
    4. Upload PNG to remote storage for future use
    5. Return PNG path

    Args:
        workspace_id: Workspace identifier
        unique_id: Experiment unique identifier
        original_path: Path to original TIFF or JSON file
        remote_bucket_name: remote storage bucket name for remote storage
        thumb_type: ThumbnailType.INPUT (for TIFF) or ThumbnailType.ROI
            (for cell_roi.json)
        dataset_paths: Internal dataset paths for structured data
            (optional)

    Returns:
        Path to the thumbnail PNG file (may be newly generated)
    """
    thumb_path = ThumbnailGenerator.get_thumbnail_path(
        workspace_id, unique_id, thumb_type
    )

    # Check if PNG thumbnail already exists
    if os.path.exists(thumb_path):
        return normalize_output_path(thumb_path)

    # Resolve the original file path
    abs_original_path = (
        ThumbnailGenerator.resolve_source_path(workspace_id, original_path)
        if original_path
        else None
    )

    # Download from remote storage if needed
    if (
        original_path
        and abs_original_path is None
        and RemoteStorageController.is_available()
    ):
        try:
            async with RemoteStorageReader(
                remote_bucket_name,
                workspace_id,
                unique_id,
                RemoteExperimentSyncMode.THUMBNAILS_ONLY,
            ) as remote_storage_controller:
                await remote_storage_controller.download_thumbnail_source(
                    workspace_id, unique_id, original_path, thumb_type
                )
            # Re-resolve after download
            abs_original_path = ThumbnailGenerator.resolve_source_path(
                workspace_id, original_path
            )
        except RemoteStorageLockError:
            # Let upstream get_thumbnail map this to HTTP 423; swallowing it
            # here would lose the lock semantics.
            raise
        except Exception as e:
            logger.warning(f"Failed to download thumbnail source: {e}")

    # Generate thumbnail. Goal: always produce a PNG at thumb_path.
    # - INPUT with source: TIFF/HDF5/MAT render or placeholder by extension.
    # - INPUT without source: labeled "INPUT" placeholder.
    # - ROI with source: render from cell_roi.json.
    # - ROI without source: labeled "ROI" placeholder.
    create_directory(os.path.dirname(thumb_path))
    wrote_placeholder = False
    try:
        if thumb_type == ThumbnailType.INPUT:
            if original_path:
                # generate_input_thumbnail returns False when it falls back to a
                # placeholder (missing source, unsupported format, render error).
                wrote_real = ThumbnailGenerator.generate_input_thumbnail(
                    source_path=original_path,
                    output_path=thumb_path,
                    abs_source_path=abs_original_path,
                    dataset_paths=dataset_paths,
                )
                wrote_placeholder = not wrote_real
            else:
                ThumbnailGenerator.generate_placeholder_thumbnail(
                    thumb_path, label="INPUT"
                )
                wrote_placeholder = True
        elif abs_original_path is not None:
            ThumbnailGenerator.generate_roi_thumbnail(abs_original_path, thumb_path)
        else:
            # No ROI source available — fall back to a labeled placeholder
            ThumbnailGenerator.generate_placeholder_thumbnail(thumb_path, label="ROI")
            wrote_placeholder = True

        logger.info(f"Lazy-generated thumbnail: {thumb_path}")

    except Exception as e:
        # Generation itself failed — write a placeholder so the retry button
        # always succeeds rather than 404'ing the caller.
        logger.warning(
            f"Thumbnail generation failed for {workspace_id}/{unique_id}/{thumb_type}; "
            f"writing placeholder. Error: {e}",
            exc_info=True,
        )
        try:
            label = "INPUT" if thumb_type == ThumbnailType.INPUT else "ROI"
            ThumbnailGenerator.generate_placeholder_thumbnail(thumb_path, label=label)
            wrote_placeholder = True
        except Exception as e2:
            logger.error(f"Even placeholder thumbnail generation failed: {e2}")
            return normalize_output_path(original_path or thumb_path)

    # Upload to remote storage for future use (fire and forget).
    # Skip placeholder uploads: caching a placeholder in S3 would prevent the
    # retry button from ever recovering if the source later becomes available
    # (e.g. after a transient download failure).
    if RemoteStorageController.is_available() and not wrote_placeholder:
        try:
            async with RemoteStorageSimpleWriter(
                remote_bucket_name
            ) as remote_storage_controller:
                await remote_storage_controller.upload_thumbnail(
                    workspace_id, unique_id, thumb_path
                )
        except Exception as e:
            logger.warning(
                f"Failed to upload generated thumbnail to remote storage: {e}"
            )

    return normalize_output_path(thumb_path)


async def _background_full_sync(
    remote_bucket_name: str, workspace_id: str, unique_id: str
) -> None:
    """
    Background task to download remaining experiment files (PKL, NWB) after
    visualization files have been loaded. This prepares the experiment for
    Edit ROI without blocking the user.
    """
    try:
        # Check if full sync is still needed
        is_unsynced = RemoteSyncStatusFileUtil.check_sync_status_unsynced(
            workspace_id, unique_id
        )

        if not is_unsynced:
            logger.debug(
                f"Background sync skipped - already synced: {workspace_id}/{unique_id}"
            )
            return

        logger.info(f"Background full sync starting for {workspace_id}/{unique_id}")

        sync_mode = RemoteExperimentSyncMode.ALL
        async with RemoteStorageReader(
            remote_bucket_name, workspace_id, unique_id, sync_mode
        ) as remote_storage_controller:
            await remote_storage_controller.download_experiment(
                workspace_id, unique_id, sync_mode=sync_mode
            )

        logger.info(f"Background full sync completed for {workspace_id}/{unique_id}")

    except Exception as e:
        # Log but don't raise - this is a background task
        logger.warning(
            f"Background full sync failed for {workspace_id}/{unique_id}: {e}"
        )


@router.post(
    "/sync/{workspace_id}/{unique_id}",
    response_model=bool,
    dependencies=[Depends(is_workspace_available)],
    description="""
    Sync visualization files (JSON, TIFF)
    from remote storage for viewing experiment results.
    Call this before loading visualization data to ensure files are available locally.
    Only syncs files needed for visualization, not large PKL/NWB files.
    Automatically triggers background sync for remaining files (PKL/NWB) for Edit ROI.
    """,
)
async def sync_visualization_files(
    workspace_id: str,
    unique_id: str,
    background_tasks: BackgroundTasks,
    remote_bucket_name: str = Depends(get_outputs_remote_bucket_name),
) -> bool:
    """
    Lazy-load visualization files from remote storage.
    Downloads only JSON and TIFF files needed for viewing results.
    Then triggers background download of PKL/NWB files for Edit ROI.
    """
    if not RemoteStorageController.is_available():
        return True  # No remote storage, files should be local

    # Check if sync is needed
    is_unsynced = RemoteSyncStatusFileUtil.check_sync_status_unsynced(
        workspace_id, unique_id
    )

    if not is_unsynced:
        return True  # Already fully synced

    logger.info(
        f"Syncing visualization files for {workspace_id}/{unique_id} "
        "from remote storage"
    )

    try:
        sync_mode = RemoteExperimentSyncMode.VISUALIZATION
        async with RemoteStorageReader(
            remote_bucket_name, workspace_id, unique_id, sync_mode
        ) as remote_storage_controller:
            result = await remote_storage_controller.download_experiment(
                workspace_id,
                unique_id,
                sync_mode=sync_mode,
            )

            # Also download input files needed for viewing images
            try:
                input_filenames = SmkUtils.get_datatypes_inputs(
                    workspace_id, unique_id, apply_basename=True
                )
                for input_filename in input_filenames:
                    await remote_storage_controller.download_input_data(
                        workspace_id, input_filename
                    )
            except (AssertionError, KeyError):
                # snakemake.yaml may be empty or missing required keys
                pass
    except RemoteExperimentNotFoundError as e:
        logger.warning(e)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except RemoteStorageLockError as e:
        logger.warning(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))

    # Trigger background task to download remaining files (PKL/NWB)
    # This prepares Edit ROI while user is viewing results
    background_tasks.add_task(
        _background_full_sync, remote_bucket_name, workspace_id, unique_id
    )

    return result


@router.get(
    "/thumbnail/{workspace_id}/{unique_id}/{thumb_type}",
    description="""
    Get a thumbnail PNG image for an experiment.
    Syncs from remote storage if not available locally,
      or generates on-demand if needed.

    Args:
        workspace_id: Workspace identifier
        unique_id: Experiment unique identifier
        thumb_type: Either "input" or "roi"

    Returns:
        PNG image file
    """,
)
async def get_thumbnail(
    workspace_id: str,
    unique_id: str,
    thumb_type: ThumbnailType,
    remote_bucket_name: str = Depends(get_outputs_remote_bucket_name),
):
    """
    Serve thumbnail PNG images for DataView.

    This endpoint handles:
    1. Syncing thumbnails from remote storage if not available locally
    2. Generating thumbnails on-demand from source files if needed
    3. Serving the PNG file with proper content type
    """

    # Get the expected thumbnail path
    thumb_path = ThumbnailGenerator.get_thumbnail_path(
        workspace_id, unique_id, thumb_type
    )

    # Try to sync thumbnail from remote storage if not available locally
    if not os.path.exists(thumb_path) and RemoteStorageController.is_available():
        try:
            sync_mode = RemoteExperimentSyncMode.THUMBNAILS_ONLY
            async with RemoteStorageReader(
                remote_bucket_name, workspace_id, unique_id, sync_mode
            ) as remote_storage_controller:
                await remote_storage_controller.download_experiment(
                    workspace_id,
                    unique_id,
                    sync_mode=sync_mode,
                )
        except RemoteExperimentNotFoundError as e:
            # Don't 404 here — fall through to generation / placeholder
            logger.warning(
                f"Experiment not found in remote storage during thumbnail sync: {e}"
            )
        except RemoteStorageLockError as e:
            logger.warning(e)
            raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))
        except Exception as e:
            logger.warning(f"Failed to sync thumbnail from remote storage: {e}")
            pass  # Continue processing

    # If thumbnail still doesn't exist, try to generate it
    if not os.path.exists(thumb_path):
        # Sync essential config files (yaml) so we can determine source paths
        # Note: thumbnails_only mode doesn't download the config files needed to
        # find the source TIFF/JSON files for generation
        if RemoteStorageController.is_available():
            try:
                sync_mode = RemoteExperimentSyncMode.ESSENTIAL_ONLY
                async with RemoteStorageReader(
                    remote_bucket_name, workspace_id, unique_id, sync_mode
                ) as remote_storage_controller:
                    await remote_storage_controller.download_experiment(
                        workspace_id,
                        unique_id,
                        sync_mode=sync_mode,
                    )
            except RemoteExperimentNotFoundError as e:
                # Don't 404 here — fall through to placeholder generation
                logger.warning(
                    f"Experiment not found in remote storage during config sync: {e}"
                )
            except RemoteStorageLockError as e:
                logger.warning(e)
                raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))
            except Exception as e:
                logger.warning(f"Failed to sync config files from remote storage: {e}")
                pass  # Continue processing

        # Get the original file path for generation.
        # If we cannot determine it, fall through with original_path=None —
        # get_or_generate_thumbnail will write a placeholder PNG.
        original_path = None
        dataset_paths = None
        if thumb_type == ThumbnailType.INPUT:
            try:
                input_filenames = SmkUtils.get_datatypes_inputs(
                    workspace_id, unique_id, apply_basename=True
                )
                if input_filenames:
                    original_path = input_filenames[0]
                else:
                    logger.warning(
                        f"No input files found for {workspace_id}/{unique_id}; "
                        "will fall back to placeholder thumbnail"
                    )
            except (AssertionError, KeyError) as e:
                logger.warning(
                    f"Could not determine input file for "
                    f"{workspace_id}/{unique_id}: {e}; "
                    "will fall back to placeholder thumbnail"
                )

            # Extract dataset paths from workflow config (optional enhancement)
            if original_path:
                try:
                    from studio.app.common.core.dataview.dataview_services import (
                        DataviewService,
                    )

                    wf_config = WorkflowConfigReader.read(workspace_id, unique_id)
                    _, dataset_paths = DataviewService.select_best_thumbnail_input(
                        wf_config
                    )
                except Exception:
                    pass  # Dataset paths are optional enhancement
        else:
            # ROI thumbnail uses cell_roi.json
            try:
                from studio.app.common.core.dataview.dataview_services import (
                    DataviewService,
                )

                thumbnails, _ = DataviewService.make_dataview_thumnail_paths(
                    workspace_id, unique_id
                )
                if thumbnails.roi_url:
                    original_path = thumbnails.roi_url
                else:
                    logger.warning(
                        f"No ROI data found for {workspace_id}/{unique_id}; "
                        "will fall back to placeholder thumbnail"
                    )
            except Exception as e:
                logger.warning(
                    f"Could not determine ROI file path for "
                    f"{workspace_id}/{unique_id}: {e}; "
                    "will fall back to placeholder thumbnail"
                )

        # Generate thumbnail (may download source from remote
        # storage if needed). get_or_generate_thumbnail returns
        # a normalized (relative) path.
        await get_or_generate_thumbnail(
            workspace_id,
            unique_id,
            original_path,
            remote_bucket_name,
            thumb_type,
            dataset_paths=dataset_paths,
        )
        # Re-get the absolute path since generation should have created the file
        thumb_path = ThumbnailGenerator.get_thumbnail_path(
            workspace_id, unique_id, thumb_type
        )

    # Final guarantee: always serve a PNG. If every prior attempt
    # failed to produce thumb_path, write a labeled placeholder here.
    if not os.path.exists(thumb_path):
        logger.warning(
            f"Thumbnail still missing after generation attempts for "
            f"{workspace_id}/{unique_id}/{thumb_type}; writing placeholder"
        )
        try:
            create_directory(os.path.dirname(thumb_path))
            label = "INPUT" if thumb_type == ThumbnailType.INPUT else "ROI"
            ThumbnailGenerator.generate_placeholder_thumbnail(thumb_path, label=label)
        except Exception as e:
            logger.error(f"Failed to write final placeholder thumbnail: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Could not produce thumbnail: {e}",
            )

    return FileResponse(
        thumb_path,
        media_type="image/png",
        filename=thumb_type.filename,
    )


async def _ensure_visualization_synced(dirpath: str, remote_bucket_name: str) -> None:
    """
    On-demand sync for visualization files.
    Extracts workspace_id and unique_id from dirpath and triggers sync if needed.
    """
    if not RemoteStorageController.is_available():
        return

    if not dirpath.startswith(DIRPATH.OUTPUT_DIR):
        return

    # Trim path to workspace_id/unique_id level
    # (ExptOutputPathIds expects 2-3 components)
    relative_path = os.path.relpath(dirpath, DIRPATH.OUTPUT_DIR)
    path_parts = relative_path.split(os.sep)
    if len(path_parts) < 2:
        return
    trimmed_path = os.path.join(DIRPATH.OUTPUT_DIR, *path_parts[:2])

    # Extract IDs from path
    path_ids = ExptOutputPathIds(trimmed_path)
    workspace_id = path_ids.workspace_id
    unique_id = path_ids.unique_id

    if not workspace_id or not unique_id:
        return

    # Check if sync is needed
    is_unsynced = RemoteSyncStatusFileUtil.check_sync_status_unsynced(
        workspace_id, unique_id
    )

    if not is_unsynced:
        return

    logger.info(f"On-demand sync for visualization: {workspace_id}/{unique_id}")

    try:
        sync_mode = RemoteExperimentSyncMode.VISUALIZATION
        async with RemoteStorageReader(
            remote_bucket_name, workspace_id, unique_id, sync_mode
        ) as remote_storage_controller:
            await remote_storage_controller.download_experiment(
                workspace_id,
                unique_id,
                sync_mode=sync_mode,
            )
            # Also download input files (if snakemake config is available)
            try:
                input_filenames = SmkUtils.get_datatypes_inputs(
                    workspace_id, unique_id, apply_basename=True
                )
                for input_filename in input_filenames:
                    await remote_storage_controller.download_input_data(
                        workspace_id, input_filename
                    )
            except (AssertionError, KeyError):
                # snakemake.yaml may be empty or missing required keys
                pass
    except RemoteExperimentNotFoundError as e:
        logger.warning(e)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except RemoteStorageLockError as e:
        logger.warning(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))


def get_initial_timeseries_data(dirpath) -> JsonTimeSeriesData:
    plot_meta_path = f"{dirpath}.plot-meta.json"
    plot_meta = JsonReader.read_as_plot_meta(plot_meta_path)

    return JsonTimeSeriesData(
        xrange=[],
        data={},
        std={},
        meta=plot_meta,
    )


def _load_timeseries_record(dirpath: str, record_id: str) -> JsonTimeSeriesData:
    """
    Load a single timeseries record from either chunked or legacy format.

    Args:
        dirpath: Directory containing the timeseries data
        record_id: Record identifier (as string)

    Returns:
        JsonTimeSeriesData for the specified record
    """
    if TimeSeriesChunkHandler.is_chunked_format(dirpath):
        # Chunked format
        cell_data = TimeSeriesChunkHandler.get_record_data(dirpath, record_id)
        # Convert from split format to DataFrame
        df = pd.DataFrame(
            cell_data["data"], index=cell_data["index"], columns=cell_data["columns"]
        )
        return JsonReader.read_as_timeseries_from_df(df)
    else:
        # Legacy format
        return JsonReader.read_as_timeseries(
            join_filepath([dirpath, f"{record_id}.json"])
        )


@router.get("/inittimedata/{dirpath:path}", response_model=JsonTimeSeriesData)
async def get_inittimedata(
    dirpath: str,
    isFull: Optional[bool] = None,
    remote_bucket_name: str = Depends(get_outputs_remote_bucket_name),
):
    # Normalize and convert to absolute path for filesystem operations
    dirpath = secure_output_relpath(dirpath)
    abs_dirpath = join_filepath([DIRPATH.OUTPUT_DIR, dirpath])

    # On-demand sync if files don't exist
    await _ensure_visualization_synced(abs_dirpath, remote_bucket_name)

    full_json_dirpath = abs_dirpath + ORIGINAL_DATA_EXT
    if isFull and os.path.exists(full_json_dirpath):
        abs_dirpath = full_json_dirpath

    file_numbers = TimeSeriesChunkHandler.get_all_record_ids(abs_dirpath)

    # Handle empty case
    if not file_numbers:
        return_data = get_initial_timeseries_data(abs_dirpath)
        return_data.meta = {"title": "0 ROIs found"}  # Set informative message
        return return_data

    # Get first cell data
    index = file_numbers[0]
    str_index = str(index)

    # Load first record using common helper
    json_data = _load_timeseries_record(abs_dirpath, str_index)

    data = {
        str(i): {json_data.xrange[0]: json_data.data[json_data.xrange[0]]}
        for i in file_numbers
    }

    if json_data.std is not None:
        std = {
            str(i): {json_data.xrange[0]: json_data.data[json_data.xrange[0]]}
            for i in file_numbers
        }

    return_data = get_initial_timeseries_data(abs_dirpath)
    return_data.xrange = json_data.xrange
    if json_data.std is not None:
        return_data.std = std

    return_data.data = data
    return_data.data[str_index] = json_data.data
    if json_data.std is not None:
        return_data.std[str_index] = json_data.std

    return return_data


@router.get("/timedata/{dirpath:path}", response_model=JsonTimeSeriesData)
async def get_timedata(
    dirpath: str,
    index: int,
    isFull: Optional[bool] = None,
    remote_bucket_name: str = Depends(get_outputs_remote_bucket_name),
):
    # Normalize and convert to absolute path for filesystem operations
    dirpath = secure_output_relpath(dirpath)
    abs_dirpath = join_filepath([DIRPATH.OUTPUT_DIR, dirpath])

    # On-demand sync if files don't exist
    await _ensure_visualization_synced(abs_dirpath, remote_bucket_name)

    full_json_dirpath = abs_dirpath + ORIGINAL_DATA_EXT
    if isFull and os.path.exists(full_json_dirpath):
        abs_dirpath = full_json_dirpath

    str_index = str(index)

    # Load record using common helper
    json_data = _load_timeseries_record(abs_dirpath, str_index)

    return_data = get_initial_timeseries_data(abs_dirpath)

    return_data.data[str_index] = json_data.data
    if json_data.std is not None:
        return_data.std[str_index] = json_data.std

    return return_data


@router.get("/alltimedata/{dirpath:path}", response_model=JsonTimeSeriesData)
async def get_alltimedata(
    dirpath: str,
    remote_bucket_name: str = Depends(get_outputs_remote_bucket_name),
):
    # Normalize and convert to absolute path for filesystem operations
    dirpath = secure_output_relpath(dirpath)
    abs_dirpath = join_filepath([DIRPATH.OUTPUT_DIR, dirpath])

    # On-demand sync if files don't exist
    await _ensure_visualization_synced(abs_dirpath, remote_bucket_name)

    return_data = get_initial_timeseries_data(abs_dirpath)

    if TimeSeriesChunkHandler.is_chunked_format(abs_dirpath):
        # Chunked format: load all chunks
        all_records = TimeSeriesChunkHandler.load_all_records(abs_dirpath)

        for cell_index, cell_data in all_records.items():
            # Convert from split format to timeseries format
            df = pd.DataFrame(
                cell_data["data"],
                index=cell_data["index"],
                columns=cell_data["columns"],
            )
            json_data = JsonReader.read_as_timeseries_from_df(df)

            if not return_data.xrange:
                return_data.xrange = json_data.xrange

            return_data.data[cell_index] = json_data.data
            if json_data.std is not None:
                if not return_data.std:
                    return_data.std = {}
                return_data.std[cell_index] = json_data.std
    else:
        # Legacy format: individual files
        from glob import glob

        metadata_files = [
            TimeSeriesChunkHandler.INDEX_MAP_FILENAME,  # chunk_index_map.json
            f"{os.path.basename(dirpath)}.plot-meta.json",
        ]
        for i, path in enumerate(glob(join_filepath([abs_dirpath, "*.json"]))):
            filename = os.path.basename(path)
            # Skip metadata files
            if filename in metadata_files:
                continue

            str_idx = str(os.path.splitext(filename)[0])
            json_data = JsonReader.read_as_timeseries(path)
            if i == 0:
                return_data.xrange = json_data.xrange

            return_data.data[str_idx] = json_data.data
            if json_data.std is not None:
                return_data.std[str_idx] = json_data.std

    return return_data


@router.get("/data/{filepath:path}", response_model=OutputData)
async def get_file(
    filepath: str,
    remote_bucket_name: str = Depends(get_outputs_remote_bucket_name),
):
    # Normalize and convert to absolute path for filesystem operations
    filepath = secure_output_relpath(filepath)
    abs_filepath = join_filepath([DIRPATH.OUTPUT_DIR, filepath])

    # On-demand sync if files don't exist
    await _ensure_visualization_synced(
        os.path.dirname(abs_filepath), remote_bucket_name
    )

    return JsonReader.read_as_output(abs_filepath)


@router.get("/html/{filepath:path}", response_model=OutputData)
async def get_html(filepath: str):
    # Normalize and convert to absolute path for filesystem operations
    filepath = secure_output_relpath(filepath)
    abs_filepath = join_filepath([DIRPATH.OUTPUT_DIR, filepath])
    return Reader.read_as_output(abs_filepath)


@router.get("/image/{filepath:path}", response_model=OutputData)
async def get_image(
    filepath: str,
    workspace_id: str,
    unique_id: Optional[str] = None,  # For published data access validation
    start_index: Optional[int] = 0,
    end_index: Optional[int] = 10,
    isFull: Optional[bool] = None,
    remote_bucket_name: str = Depends(get_outputs_remote_bucket_name),
):
    # Normalize filepath for backward compatibility with existing DB records
    # that may contain absolute paths like /app/studio_data/output/...
    workspace_id = secure_component(workspace_id)
    filepath = secure_output_relpath(filepath)

    # Convert to absolute path for filesystem operations
    abs_filepath = join_filepath([DIRPATH.OUTPUT_DIR, filepath])

    # On-demand sync if files don't exist
    await _ensure_visualization_synced(
        os.path.dirname(abs_filepath), remote_bucket_name
    )

    filename, ext = os.path.splitext(os.path.basename(filepath))

    if filename == "cell_roi" and isFull:
        full_cell_roi_filepath = abs_filepath + ORIGINAL_DATA_EXT
        if os.path.exists(full_cell_roi_filepath):
            abs_filepath = full_cell_roi_filepath

    if ext in ACCEPT_FILE_EXT.TIFF_EXT.value:
        # Check if this is an input file (just filename)
        # vs output file (has workspace path)
        is_input_file = not filepath.startswith(f"{workspace_id}/")
        if is_input_file:
            abs_filepath = join_filepath([DIRPATH.INPUT_DIR, workspace_id, filepath])

            # On-demand sync for input files using shared helper
            try:
                synced = await RemoteStorageDownloadUtils.ensure_input_file_synced(
                    workspace_id, filename + ext, remote_bucket_name
                )
            except Exception as e:
                logger.error(f"Failed to sync input image: {e}")
                raise HTTPException(
                    status_code=503,
                    detail="Failed to sync input file from cloud storage",
                )
            if not synced:
                raise HTTPException(
                    status_code=404,
                    detail=f"Input image file not found: {filename}{ext}",
                )

        save_dirpath = join_filepath(
            [
                os.path.dirname(abs_filepath),
                filename,
            ]
        )
        json_filepath = join_filepath(
            [save_dirpath, f"{filename}_{str(start_index)}_{str(end_index)}.json"]
        )
        if not os.path.exists(json_filepath):
            save_tiff2json(abs_filepath, save_dirpath, start_index, end_index)
    else:
        json_filepath = abs_filepath
        # Check if output file exists after sync attempt
        if not os.path.exists(json_filepath):
            if remote_bucket_name:
                logger.warning(f"File not found after sync attempt: {json_filepath}")
                experiment_dir = os.path.dirname(os.path.dirname(json_filepath))
                if os.path.exists(experiment_dir):
                    raise HTTPException(
                        status_code=404,
                        detail="Output file not found. "
                        "Analysis may not have generated this file.",
                    )
                else:
                    raise HTTPException(
                        status_code=503,
                        detail="Data syncing. Please retry.",
                    )
            raise HTTPException(
                status_code=404,
                detail="Output file not found",
            )

    return JsonReader.read_as_output(json_filepath)


@router.get("/csv/{filepath:path}", response_model=OutputData)
async def get_csv(
    filepath: str,
    workspace_id: str,
    remote_bucket_name: str = Depends(get_outputs_remote_bucket_name),
):
    workspace_id = secure_component(workspace_id)
    filepath = secure_relpath(
        join_filepath([DIRPATH.INPUT_DIR, workspace_id]), filepath
    )
    original_filename = os.path.basename(filepath)
    abs_filepath = join_filepath([DIRPATH.INPUT_DIR, workspace_id, filepath])

    # On-demand sync for input files using shared helper
    try:
        synced = await RemoteStorageDownloadUtils.ensure_input_file_synced(
            workspace_id, original_filename, remote_bucket_name
        )
    except Exception as e:
        logger.error(f"Failed to sync input CSV: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to sync input file from cloud storage",
        )

    # Check file exists before reading (prevents bare 500 from FileNotFoundError)
    if not synced or not os.path.exists(abs_filepath):
        raise HTTPException(
            status_code=404,
            detail=f"Input CSV file not found: {original_filename}",
        )

    filename, _ = os.path.splitext(os.path.basename(abs_filepath))
    save_dirpath = join_filepath([os.path.dirname(abs_filepath), filename])
    create_directory(save_dirpath)
    json_filepath = join_filepath([save_dirpath, f"{filename}.json"])

    JsonWriter.write_as_split(json_filepath, pd.read_csv(abs_filepath, header=None))
    return JsonReader.read_as_output(json_filepath)


@router.get("/structured/{workspace_id}/{unique_id}/{node_id}")
async def get_structured_data(
    workspace_id: str,
    unique_id: str,
    node_id: str,
    start_index: Optional[int] = 0,
    end_index: Optional[int] = 10,
    remote_bucket_name: str = Depends(get_outputs_remote_bucket_name),
):
    try:
        config = WorkflowConfigReader.read(workspace_id, unique_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Workflow config not found")

    node = config.nodeDict.get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")

    file_path = node.data.path
    if isinstance(file_path, list):
        file_path = file_path[0] if file_path else None
    if not file_path:
        raise HTTPException(status_code=400, detail="Node has no file path")

    full_path = join_filepath([DIRPATH.INPUT_DIR, workspace_id, file_path])
    if not os.path.exists(full_path):
        # Inputs are cleared independently of outputs, so re-fetch keyed on the
        # file itself, not the experiment's output-sync status.
        try:
            await RemoteStorageDownloadUtils.ensure_input_file_synced(
                workspace_id, file_path, remote_bucket_name
            )
        except Exception as e:
            logger.error(f"Failed to sync input data: {e}")
            raise HTTPException(
                status_code=503,
                detail="Failed to sync input file from cloud storage",
            )
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    hdf5_path = node.data.hdf5Path
    mat_path = node.data.matPath

    try:
        if hdf5_path is not None:
            with h5py.File(full_path, "r") as f:
                dataset = f[hdf5_path]
                shape = dataset.shape
                ndim = dataset.ndim
                if ndim == 3:
                    si = max(0, start_index)
                    ei = min(shape[0], end_index)
                    data = dataset[si:ei]
                else:
                    data = dataset[:]
        elif mat_path is not None:
            raw = MatGetter.data(full_path, mat_path)
            data = np.asarray(raw)
            shape = data.shape
            ndim = data.ndim
            if ndim == 3:
                si = max(0, start_index)
                ei = min(shape[0], end_index)
                data = data[si:ei]
        else:
            raise HTTPException(
                status_code=400,
                detail="Node has no hdf5Path or matPath",
            )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Dataset not found: {e}")

    data = np.asarray(data)
    dataset_path = hdf5_path or mat_path

    match ndim:
        case 3:
            return {
                "data": data.tolist(),
                "data_type": "images",
                "total_frames": int(shape[0]),
                "dataset_path": dataset_path,
            }
        case 2:
            df = pd.DataFrame(data)
            return {
                "data": df.to_dict(orient="split")["data"],
                "columns": [str(c) for c in df.columns.tolist()],
                "index": [str(i) for i in df.index.tolist()],
                "data_type": "timeseries",
                "dataset_path": dataset_path,
            }
        case 1:
            return {
                "data": data.tolist(),
                "index": list(range(len(data))),
                "data_type": "bar",
                "dataset_path": dataset_path,
            }
        case _:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported data dimensionality: {ndim}",
            )
