Docker
=================

```{contents}
:depth: 4
```

## Installation

We introduce how to install optinist.
We have developed optinist python(backend) and typescript(frontend), so you need to make both environment.
Please follow instructions below.

<br />

## 1. Make Docker Image Container

### Install Tools

- Install [Docker](https://www.docker.com/products/docker-desktop/)

### Make Docker Image

Pull the latest docker image from docker hub.

```bash
docker pull oistncu/optinist
```

Start docker container.

```bash
# Notes:
# - Set your saving directory to `/your/saving/dir`.
# - The "command line continuation character" should be replaced by your platform.
#   - Unix Shells ... \
#   - Windows Command Prompt ... ^
#   - Windows PowerShell ... `

docker run -it --shm-size=2G \
  -v /your/saving/dir:/app/studio_data \
  --env OPTINIST_DIR="/app/studio_data" \
  --name optinist_container -d -p 8000:8000 --restart=unless-stopped \
  oistncu/optinist:latest \
  poetry run python main.py --host 0.0.0.0 --port 8000
```

```{eval-rst}
.. note::
    * Please set ``/your/saving/dir`` to your local directory.
      Without this, OptiNiSt's data will lost when you stop docker container or reboot your PC.
      By the command above (``-v`` option), you can mount your local directory to docker container.
    * If you want to keep the snakemake directory (which contains the conda environment etc.) on the host machine, add the following option:
      ``-v /your/snakemake/dir:/app/.snakemake``
```

## 2. Access to Backend

- Launch browser, and go to `http://localhost:8000`

Done!
