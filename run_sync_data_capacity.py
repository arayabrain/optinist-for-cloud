import argparse

from sqlmodel import select

from studio.app.common.core.workspace.workspace_services import WorkspaceService
from studio.app.common.db.database import session_scope
from studio.app.common.models.workspace import Workspace


def main(args):
    if WorkspaceService.is_data_usage_available():
        with session_scope() as db:
            workspace_list = db.execute(
                select(Workspace.id).filter(Workspace.deleted.is_(False))
            ).scalars()
            for workspace_id in workspace_list:
                WorkspaceService.update_workspace_data_usage(db, str(workspace_id))
                WorkspaceService.sync_workspace_experiment(db, str(workspace_id))
    else:
        WorkspaceService.sync_workspace_experiment(db=None, workspace_id="1")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    main(parser.parse_args())
