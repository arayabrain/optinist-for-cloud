import {
  configureStore,
  ThunkAction,
  Action,
  combineReducers,
} from "@reduxjs/toolkit"

import { analyticsMiddleware } from "store/analyticsMiddleware"
import {
  algorithmListReducer,
  algorithmNodeReducer,
  displayDataReducer,
  fileUploaderReducer,
  flowElementReducer,
  inputNodeReducer,
  nwbReducer,
  filesTreeReducer,
  handleTypeColorReducer,
  rightDrawerReducer,
  visualaizeItemReducer,
  snakemakeReducer,
  pipelineReducer,
  hdf5Reducer,
  matlabReducer,
  experimentsReducer,
  workspaceReducer,
  userReducer,
  modeStandalone,
  logsModalReducer,
  subscriptionReducer,
  dataviewReducer,
  paymentMethodReducer,
  RegistrationReducer,
} from "store/slice"
import { DATAVIEW_SLICE_NAME } from "store/slice/Dataview/DataviewType"

export const rootReducer = combineReducers({
  algorithmList: algorithmListReducer,
  algorithmNode: algorithmNodeReducer,
  displayData: displayDataReducer,
  fileUploader: fileUploaderReducer,
  flowElement: flowElementReducer,
  inputNode: inputNodeReducer,
  handleColor: handleTypeColorReducer,
  filesTree: filesTreeReducer,
  nwb: nwbReducer,
  rightDrawer: rightDrawerReducer,
  visualaizeItem: visualaizeItemReducer,
  snakemake: snakemakeReducer,
  pipeline: pipelineReducer,
  hdf5: hdf5Reducer,
  matlab: matlabReducer,
  experiments: experimentsReducer,
  workspace: workspaceReducer,
  user: userReducer,
  mode: modeStandalone,
  logsModal: logsModalReducer,
  [DATAVIEW_SLICE_NAME]: dataviewReducer,
  subscription: subscriptionReducer,
  paymentMethod: paymentMethodReducer,
  registration: RegistrationReducer,
})

export const store = configureStore({
  reducer: rootReducer,
  middleware: (getDefaultMiddleware) =>
    getDefaultMiddleware({
      // Ignore serializable checks because serializing fails
      // on rejectedWith value
      serializableCheck: false,
    }).concat(analyticsMiddleware),
})

export type AppDispatch = typeof store.dispatch
export type RootState = ReturnType<typeof store.getState>
export type AppThunk<ReturnType = void> = ThunkAction<
  ReturnType,
  RootState,
  unknown,
  Action<string>
>
export type ThunkApiConfig<T = unknown, PendingMeta = unknown> = {
  state: RootState
  dispatch: AppDispatch
  rejectValue: T
  penfingMeta: PendingMeta
}
