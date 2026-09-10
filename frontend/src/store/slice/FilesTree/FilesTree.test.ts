import { expect, describe, test, jest } from "@jest/globals"

import { FILE_TREE_TYPE_SET, TreeNodeTypeDTO } from "api/files/Files"
import { getFilesTree, deleteFile } from "store/slice/FilesTree/FilesTreeAction"
import reducer, { initialState } from "store/slice/FilesTree/FilesTreeSlice"
import { FilesTree } from "store/slice/FilesTree/FilesTreeType"
import { uploadFile } from "store/slice/FileUploader/FileUploaderActions"

// notistack 3 assigns its standalone `enqueueSnackbar` inside SnackbarProvider's
// constructor, so it is undefined until a provider mounts and the real one would
// throw in a reducer test. Mocking it also makes the notice assertable, which is
// the half of a failed fetch the flags cannot show.
// The factory runs while the slice is being imported, before the const below is
// initialised, so it hands over a wrapper that resolves the spy on call instead.
jest.mock("notistack", () => ({
  enqueueSnackbar: (...args: unknown[]) => mockEnqueueSnackbar(...args),
}))
const mockEnqueueSnackbar = jest.fn()

describe("FilesTree", () => {
  const mockPayload: TreeNodeTypeDTO[] = [
    {
      path: "/tmp/optinist/input/hoge",
      name: "hoge",
      isdir: true,
      shape: [],
      nodes: [
        {
          path: "/tmp/optinist/input/hoge/hoge.tif",
          name: "hoge.tif",
          isdir: false,
          shape: [],
        },
      ],
    },
    {
      path: "/tmp/optinist/input/copy_image1",
      name: "copy_image1",
      isdir: true,
      shape: [],
      nodes: [
        {
          path: "/tmp/optinist/input/copy_image1/copy_image1.tif",
          name: "copy_image1.tif",
          isdir: false,
          shape: [],
        },
      ],
    },
  ]

  const expectState: FilesTree = {
    image: {
      isLoading: false,
      isLatest: true,
      tree: [
        {
          path: "/tmp/optinist/input/hoge",
          name: "hoge",
          isDir: true,
          shape: [],
          nodes: [
            {
              path: "/tmp/optinist/input/hoge/hoge.tif",
              name: "hoge.tif",
              isDir: false,
              shape: [],
            },
          ],
        },
        {
          path: "/tmp/optinist/input/copy_image1",
          name: "copy_image1",
          isDir: true,
          shape: [],
          nodes: [
            {
              path: "/tmp/optinist/input/copy_image1/copy_image1.tif",
              name: "copy_image1.tif",
              isDir: false,
              shape: [],
            },
          ],
        },
      ],
    },
  }

  const pendingAction = {
    type: getFilesTree.pending.type,
    meta: {
      arg: { fileType: "image" },
      requestId: "F0QeIMS-KV132B2q79qaz",
      requestStatus: "pending",
    },
  }

  const fulfilledAction = {
    type: getFilesTree.fulfilled.type,
    payload: mockPayload,
    meta: {
      arg: { fileType: "image" },
      requestId: "F0QeIMS-KV132B2q79qaz",
      requestStatus: "fulfilled",
    },
  }

  const rejectedAction = {
    type: getFilesTree.rejected.type,
    payload: new Error("network"),
    error: { message: "Rejected" },
    meta: {
      arg: { fileType: "image" },
      requestId: "F0QeIMS-KV132B2q79qaz",
      requestStatus: "rejected",
      rejectedWithValue: true,
    },
  }

  test(getFilesTree.fulfilled.type, () => {
    expect(
      reducer(reducer(initialState, pendingAction), fulfilledAction),
    ).toEqual(expectState)
  })

  // The file-tree progress bar renders off `isLoading`, so an on-demand sync of
  // data that is not cached locally shows progress only while this flag is set.
  describe("sync progress flag", () => {
    test("the fetch raises it before the tree arrives", () => {
      const state = reducer(initialState, pendingAction)

      expect(state.image.isLoading).toBe(true)
      // Marked out of date at the same time, or a component reading the cache
      // would render the stale tree as current
      expect(state.image.isLatest).toBe(false)
    })

    test("the flag clears once the tree arrives", () => {
      const state = reducer(
        reducer(initialState, pendingAction),
        fulfilledAction,
      )

      expect(state.image.isLoading).toBe(false)
      expect(state.image.isLatest).toBe(true)
    })

    test("one file type's fetch does not disturb another's flags", () => {
      const imageLoaded = reducer(
        reducer(initialState, pendingAction),
        fulfilledAction,
      )

      const state = reducer(imageLoaded, {
        ...pendingAction,
        meta: { ...pendingAction.meta, arg: { fileType: "csv" } },
      })

      expect(state.csv.isLoading).toBe(true)
      // The dialogs are per file type; shared flags would spin the image
      // dialog's bar, and mark its loaded tree stale, because a CSV tree was
      // being fetched somewhere else
      expect(state.image.isLoading).toBe(false)
      expect(state.image.isLatest).toBe(true)
      expect(state.image.tree).toEqual(expectState.image.tree)
    })

    test("the flag clears when the fetch fails", () => {
      const state = reducer(
        reducer(initialState, pendingAction),
        rejectedAction,
      )

      // Left set, the progress bar spins forever and the user is told nothing
      expect(state.image.isLoading).toBe(false)
    })

    test("a failed fetch does not leave the tree due for another attempt", () => {
      const state = reducer(
        reducer(initialState, pendingAction),
        rejectedAction,
      )

      // `useFileTree` refetches whenever `!isLatest && !isLoading`, so clearing
      // the loading flag without this would turn one failure into a request loop
      expect(state.image.isLatest).toBe(true)
    })

    test("a failed refetch keeps the tree already on screen", () => {
      const loaded = reducer(
        reducer(initialState, pendingAction),
        fulfilledAction,
      )

      const state = reducer(reducer(loaded, pendingAction), rejectedAction)

      // Blanking the dialog on a failed refresh would lose files that are still
      // there
      expect(state.image.tree).toEqual(expectState.image.tree)
    })

    test("a failed fetch says so instead of just stopping", () => {
      reducer(reducer(initialState, pendingAction), rejectedAction)

      expect(mockEnqueueSnackbar).toHaveBeenCalledWith("Failed to load files", {
        variant: "error",
      })
    })

    test("a successful fetch raises no error notice", () => {
      reducer(reducer(initialState, pendingAction), fulfilledAction)

      expect(mockEnqueueSnackbar).not.toHaveBeenCalled()
    })
  })

  // Deleting and uploading both mutate the same cache the dialogs read, and
  // neither had any coverage.
  describe("removing a file from the cached tree", () => {
    const loadedState = () =>
      reducer(reducer(initialState, pendingAction), fulfilledAction)

    const deleteAction = (fileName: string, status = "fulfilled") => ({
      type: deleteFile[status === "fulfilled" ? "fulfilled" : "rejected"].type,
      meta: {
        arg: { fileType: "image", fileName },
        requestId: "delete-1",
        requestStatus: status,
      },
    })

    test("the deleted file is dropped without a refetch", () => {
      const state = reducer(
        loadedState(),
        deleteAction("/tmp/optinist/input/hoge"),
      )

      expect(state.image.tree.map((node) => node.path)).toEqual([
        "/tmp/optinist/input/copy_image1",
      ])
      // Already current, so the dialog does not flash a reload for a change it
      // has just been handed
      expect(state.image.isLatest).toBe(true)
      expect(state.image.isLoading).toBe(false)
    })

    test("a file nested inside a directory is dropped too", () => {
      // The tree is recursive, so a delete that only filtered the top level
      // would leave the file on screen under its folder
      const state = reducer(
        loadedState(),
        deleteAction("/tmp/optinist/input/hoge/hoge.tif"),
      )

      const hoge = state.image.tree.find(
        (node) => node.path === "/tmp/optinist/input/hoge",
      )
      expect(hoge?.isDir && hoge.nodes).toEqual([])
      expect(state.image.tree).toHaveLength(2)
    })

    test("a failed delete keeps the file and says so", () => {
      const state = reducer(
        reducer(loadedState(), {
          ...deleteAction("/tmp/optinist/input/hoge", "pending"),
          type: deleteFile.pending.type,
        }),
        deleteAction("/tmp/optinist/input/hoge", "rejected"),
      )

      expect(state.image.tree.map((node) => node.path)).toEqual([
        "/tmp/optinist/input/hoge",
        "/tmp/optinist/input/copy_image1",
      ])
      expect(state.image.isLoading).toBe(false)
      // Same reasoning as a failed fetch: false here is a refetch loop
      expect(state.image.isLatest).toBe(true)
      expect(mockEnqueueSnackbar).toHaveBeenCalledWith(
        "Failed to delete file",
        {
          variant: "error",
        },
      )
    })
  })

  describe("uploading a file", () => {
    const uploadAction = (
      status: "pending" | "fulfilled",
      fileType?: string,
    ) => ({
      type: uploadFile[status].type,
      meta: {
        arg: { fileType },
        requestId: "upload-1",
        requestStatus: status,
      },
    })

    test("the matching tree is marked stale so the new file is picked up", () => {
      const loaded = reducer(
        reducer(initialState, pendingAction),
        fulfilledAction,
      )

      const state = reducer(loaded, uploadAction("fulfilled", "image"))

      // Stale rather than refetched here: useFileTree does the fetch, and this
      // flag is the only thing that asks it to
      expect(state.image.isLatest).toBe(false)
    })

    test("an upload with no file type falls back to the shared tree", () => {
      const seeded = reducer(initialState, {
        ...pendingAction,
        meta: {
          ...pendingAction.meta,
          arg: { fileType: FILE_TREE_TYPE_SET.ALL },
        },
      })

      const state = reducer(seeded, uploadAction("fulfilled"))

      expect(state[FILE_TREE_TYPE_SET.ALL].isLatest).toBe(false)
    })

    test("an unknown file type falls back rather than throwing", () => {
      // getTreeType throws for a type it does not know; the reducer must not
      // propagate that, or one bad upload takes the whole store down
      const seeded = reducer(initialState, {
        ...pendingAction,
        meta: {
          ...pendingAction.meta,
          arg: { fileType: FILE_TREE_TYPE_SET.ALL },
        },
      })

      const state = reducer(seeded, uploadAction("pending", "not-a-file-type"))

      expect(state[FILE_TREE_TYPE_SET.ALL].isLoading).toBe(true)
      expect(state[FILE_TREE_TYPE_SET.ALL].isLatest).toBe(false)
    })
  })
})
