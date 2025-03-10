# Add your Algorithm

```{contents}
:depth: 3
```

## Example of Algorithm Addition Procedure

Below we describe an example procedure for adding a new algorithm.

**Prerequisite**

- Sample Algorithm Function Name ... `my_function`
- {OPTINIST_SRC_DIR} ... Replace with the actual source storage directory path.

### 1. Prepare Necessary Directories and Files for the Algorithm

First, prepare the necessary directories and files for the algorithm.

- {OPTINIST_SRC_DIR}/studio/app/optinist/wrappers/

  - \_\_init\_\_.py
  - xxxx/
  - yyyy/
  - ...
  - custom
    - \_\_init\_\_.py
    - `my_function.py` (\*1)
    - ... (\*2)

- Explanation:
  - (\*1) Empty first.
  - (\*2) Prepare other files to be added.

### 2. Algorithm implementation

#### Import Statement Description

- Target file
  - {OPTINIST_SRC_DIR}/studio/app/optinist/wrappers/custom/`my_function`.py

```python
from studio.app.common.dataclass import *
```

- Explanation:

  - If the required dataclass does not exist, you can add your own.
  - see. [DataClass](#dataclass)

- Cautions:
  - Error might show because dataclass is not specifieid. Please fix it with correct dataclass your using.

#### Define the Input/Output of the Function and Implement the Logic.

- Target file
  - {OPTINIST_SRC_DIR}/studio/app/optinist/wrappers/custom/`my_function`.py

The function code is described below.

```python
def my_function(                 # (*1)
        image_data: ImageData,   # (*2)
        output_dir: str,         # (*3)
        params: dict=None,       # (*4)
        **kwargs,                # (*3)
    ) -> dict(                   # (*5)
      fluo=FluoData,
      image=ImageData,
      heatmap=HeatMapData):
    import numpy as np
    info = {
        "fluo": FluoData(np.random.rand(100, 20), file_name="fluo"),
        "image": ImageData(np.random.rand(10, 512, 512), file_name="image"),
        "heatmap": HeatMapData(example_analysis, file_name="heatmap")
    }
    return info
```

- Explanation:
  - (\*1) Function name can be any content.
  - (\*2) The first argument specifies the input data type. (This is also reflected in the GUI.)
  - (\*3) Add these arguments. These arguments are required for the handling workflow.
  - (\*4) This argument receives the function parameters.
    - see. [Function Parameter Definitions](#function-parameter-definitions)
  - (\*5) The return value is a dictionary type. (This is also reflected in the GUI.)

#### Definition of Information to be Displayed in the GUI

- Target file
  - {OPTINIST_SRC_DIR}/studio/app/optinist/wrappers/custom/\_\_init\_\_.py

```python
from studio.app.optinist.wrappers.custom.my_function import my_function

custom_wrapper_dict = {                       # (*1)
    'custom_node': {                               # (*2)
        'my_function': {                      # (*3)
            'function': my_function,          # (*4)
            'conda_name': 'my_function',           # (*5)
        },
    }
}
```

- Explanation:
  - (\*1) The variable name is arbitrary, but `{algorithm_name}_wrapper_dict` is the standard.
  - (\*2) Algorithm name can be any text (display label to GUI)
  - (\*3) Algorithm function name can be any text (display label to GUI)
  - (\*4) Algorithm function name specifies the actual function name
  - (\*4, 5) The conda setting is optional (to be defined when using conda with snakemake)
  - (\*5) Your Node will be save and run with the environment set here

After the registration process up to this point, restart the application browser or click the refresh button beside the node title to confirm that the algorithm has been added.

## Detailed Specifications

### DataClass

Optinist defines several DataClasses to ensure consistency between Input and Output types. The main data types are as follows. These correspond to the color of each Node's handle.

Optinist support datatype.

- ImageData
- TimeSeriesData
- FluoData
- BehaviorData
- IscellData
- Suite2pData
- ScatterData
- BarData

### Input & Output Handles

In the following example, the **my_function** function takes **ImageData** and returns [**FluoData**, **ImageData**, **HeatMapData**].

```python
from studio.app.common.dataclass import *

def my_function(
        image_data: ImageData,
        output_dir: str,
        params: dict=None,
        **kwargs,
    ) dict(fluo=FluoData, image=ImageData, heatmap=HeatMapData):
    return
```

Restart the Application and drag your new **custom_node** on the GUI, hover over the inputs and outputs to see the types.

![](../_static/add_algorithm/input_output.png)

### Function Parameter Definitions

Default function parameters can be defined in the following file. The user can then update these in the GUI.

- {OPTINIST_SRC_DIR}/studio/app/optinist/wrappers/`custom_node`/params/{algorithm_function_name}.yaml

- Sample:

  ```yaml
  window_size: 10
  custom_params_nested:
    threshold: 0.5
  ```

- Explanation:
  - {algorithm_function_name} must match the actual function name.

### Drawing Output Results

- Above we described the node input and output handle, here we describe the visualization of the result.
- The output of the function is a dictionary. (Here we use the variable **info**.)
- First, the **fluo** variable that is the return value of the **my_function function** is output by Wrap with **FluoData**. The name of the key in this case must match the **fluo** of the return value when declaring the function.
- In addition, variables to be visualized are wrapped with their data types and output. In this example, **ImageData** and **HeatMap** are output.

```python
def my_function(
        image_data: ImageData,
        output_dir: str,
        params: dict=None,
        **kwargs,
    ) dict(fluo=FluoData, image=ImageData, heatmap=HeatMapData):
    import numpy as np
    info = {
        "fluo": FluoData(np.random.rand(100, 20), file_name="fluo"),
        "image": ImageData(np.random.rand(10, 512, 512) output_dir=output_dir, file_name="image"),
        "heatmap": HeatMapData(example_analysis, file_name="heatmap")
    }

    # Prepare NWB file structure
    nwb_output = {}
    nwb_output[NWBDATASET.POSTPROCESS] = {
        "analysis_result": {  # Use a string as the key
            "data": example_analysis  # Your analysis outputs
        }
    }
    return info
```

Restart the Application, connect imageNode and run it, and you will see the output as follows.

- Note:
  - This is a quick process (only a few seconds), so if the process does not terminate, an error may have occurred. If the error persists, please submit a question to the issue.

![](../_static/add_algorithm/run.png)

![](../_static/add_algorithm/visualize_output.png)

# Add section on conda env and check yaml parameters adding correctly, function_id

#### Customize Plot Metadata

You can set plot title and axis labels to some output.

![](../_static/add_algorithm/heatmap_with_metadata.png)

To do this,

1. import PlotMetaData in the algorithm function file.
2. Add PlotMetaData to the output dataclass's `meta` attribute with title or labels you want. If you need only one of them, you can omit the other attributes.

   ```python
   from studio.app.common.schemas.outputs import PlotMetaData
   ```

def my_function( # Required inputs
neural_data: ImageData, # Fluorescence data from previous processing
output_dir: str, # Directory to save output files # Optional inputs # iscell: IscellData = None, # Cell classification data if needed
params: dict = None, # Additional parameters to customize processing
\*\*kwargs # Catch-all for additional arguments # Function returns a dictionary containing all outputs
) -> dict(fluo=FluoData, image=ImageData, heatmap=HeatMapData):

      """Example template for creating analysis functions.

      This function shows the basic structure for creating analysis functions
      that work with the pipeline, including proper input handling, NWB file
      creation, and return format.

      Args:
          neural_data: Fluorescence data from previous processing steps
          output_dir: Directory where output files should be saved
          iscell: Optional cell classification data
          params: Optional dictionary of parameters to customize processing
          **kwargs: Additional keyword arguments

      Returns:
          dict: Dictionary containing all output data and metadata
      """

      # 1. Set up logging if needed
      logger.info("Starting my_analysis_function")

      # 2. Get additional data from kwargs if needed
      # nwbfile = kwargs.get("nwbfile", {})

      # 3. Set default parameters and update with user params
      default_params = {"window_size": 10, "threshold": 0.5}
      if params is not None:
          default_params.update(params)

      # 4. Main analysis code goes here
      example_fluo_data = np.random.rand(100, 20)  # Example data

      example_imaging_data = np.random.rand(10, 512, 512)

      example_processing = example_fluo_data > default_params["threshold"]

      example_analysis = np.corrcoef(example_processing)
      for i in range(example_analysis.shape[0]):
          example_analysis[i, i] = np.nan

      # 5. Prepare NWB file structure
      # Create a new NWB file dictionary or update existing one
      nwb_output = {}

      # Add ROIs if your analysis creates them
      nwb_output[NWBDATASET.ROI] = {}  # List of ROI dictionaries

      # Example of adding processing results
      nwb_output[NWBDATASET.POSTPROCESS] = {
          "analysis_result": {  # Use a string as the key
              "data": example_analysis[0]  # Your analysis outputs
          }
      }

      # Example of data in column format (e.g. for classifications)
      nwb_output[NWBDATASET.COLUMN] = {
          "my_classification": {
              "name": "my_classification",
              "description": "Description of the classification",
              "data": example_analysis[1],
          }
      }

      # 6. Prepare return dictionary
      # This should contain all outputs and processed data
      info = {
          "fluo": FluoData(example_fluo_data, file_name="fluo"),
          "image": ImageData(example_imaging_data, file_name="image"),
          "heatmap": HeatMapData(example_analysis, file_name="heatmap"),
      }

      return info

````

```{eval-rst}
.. note::
    Following dataclasses are not supported to visualize these metadata.

    - CsvData
    - HTMLData
````
