(debugging)=
Debugging
=================
This section describes the various debugging methods included in OptiNiSt.

### Setup Conda Environment

#### 1. Preparing the Node for Conda Setup

Before setting up the Conda environment, ensure that the node is ready.

<p align="left">
  <img src="../_static/other/snakemake_node_ready_first.png" alt="Node Ready for Conda Setup" />
</p>

#### 2. Checking for an Existing Conda Environment

- If a Conda environment is not installed on the node, a message will indicate that Conda is not available.

<p align="center">
  <img width="400px" src="../_static/other/snakemake_node_ready_second.png" alt="No Conda Environment Installed" />
</p>

#### 3. Automatically Reproducing the Setup Environment

- Clicking the **"i"** button will open a modal asking if you want to automatically set up the environment for the selected node.
- To proceed with the setup, click **"CREATE ENV"** to reproduce and configure the Conda environment automatically.
- If you do not wish to set up the environment, click **"SKIP"** to bypass this step.

<p align="left">
  <img src="../_static/other/snakemake_node_ready_third.png" alt="Reproduce Conda Setup" />
</p>

<!-- ## 3. IPython notebooks

OptiNiSt provides several ipynb notebooks in the notebooks folder: caiman.ipynb, suite2p.ipynb, lccd.ipynb. These may be used for assessing where in the code

### Parameter conversion notebook
In the upgrade to OptiNiSt version 2, the parameter input structure was reorganised. Workflows created in  OptiNiSt version 1 and reproduced in version 2, as well as [workflow.yaml](ImportWorkflowYaml) produced and saved in version 1 and imported in version 2, will not work.

To reproduce a version 1 Workflow, a conversion script is provided, in the form of a IPython notebook. Follow this procedure:
1. Download workflow from Record tab
2. Open notebooks/yaml-converter.ipynb
3. Setup environment following instructions at the top of yaml-converter.ipynb
4. Convert files using the section at the bottom of yaml-converter.ipynb
```python
input_file = ".yaml"
output_file = ".yaml" # any name you want
convert_workflow_file(input_file, output_file)
``` -->

## OptiNiSt wiki FAQ

For responses to common error messages, check the [OptiNiSt Wiki FAQ](https://github.com/oist/optinist/wiki/FAQ) page, and existing issues on [OptiNiSt git](https://github.com/oist/optinist/issues). Please create an new issue describing anything not covered on these pages.
