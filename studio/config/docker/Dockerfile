FROM --platform=linux/amd64 python:3.11-slim

WORKDIR /app

# install default packages
RUN apt-get --allow-releaseinfo-change update && \
    apt-get install --no-install-recommends -y sudo procps iproute2 iputils-ping telnet wget curl unzip less vim git \
        default-mysql-client

# install conda & packages
RUN apt-get install --no-install-recommends -y gcc g++ clang clang libgl1 libgl1-mesa-dev libopencv-dev rsync && \
    apt-get autoremove -y && apt-get clean

# Specify compiler (use clang)
ENV CC=clang CXX=clang++

# NOTE: Do NOT add `conda upgrade -y --all` here.
# It upgrades conda itself along with all packages, which can break the conda
# installation (ModuleNotFoundError: No module named 'conda').
# The latest Miniforge3 installer already provides up-to-date packages.
RUN mkdir -p /opt/miniforge && \
    curl -L "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh" -o /opt/miniforge/Miniforge3.sh && \
    bash /opt/miniforge/Miniforge3.sh -b -u -p /opt/miniforge && \
    rm /opt/miniforge/Miniforge3.sh && \
    export PATH="$PATH:/opt/miniforge/bin" && \
    conda config --set channel_priority flexible && \
    conda clean -y --all
ENV PATH $PATH:/opt/miniforge/bin

# install aws packages
RUN cd /tmp && \
    wget https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip && \
    unzip awscli-exe-linux-x86_64.zip && \
    sh ./aws/install

# cleaning apt
RUN apt-get purge wget -y && \
    apt-get autoremove -y && apt-get clean

# install optimization solver (after autoremove to prevent removal)
RUN apt-get install --no-install-recommends -y coinor-cbc && \
    apt-get clean

# setup user profile
RUN echo 'alias ll="ls -la --color=auto"' >> /root/.bashrc && \
    echo 'set nu ic hls nowrap ts=4 sw=4 | syntax on' >> /root/.vimrc

# setup optinist
COPY pyproject.toml poetry.lock ./
RUN pip3 install --no-cache-dir --upgrade pip && \
    pip3 install poetry && \
    poetry config virtualenvs.create false
RUN poetry install --no-root

# setup optinist: download caiman model files
COPY studio/app/optinist/wrappers/caiman/run_download_model_files.sh ./
RUN bash run_download_model_files.sh && rm run_download_model_files.sh

# copy optinist files
COPY frontend/build /app/frontend/build
COPY studio /app/studio
COPY sample_data /app/sample_data
COPY infrastructure/scripts /app/scripts
COPY infrastructure/aws_constants.py /app/
COPY infrastructure/terraform/premium_manager_package /app/terraform/premium_manager_package
COPY infrastructure/terraform/premium_cleanup_package /app/terraform/premium_cleanup_package
COPY infrastructure/terraform/free_manager_package /app/terraform/free_manager_package
COPY infrastructure/terraform/free_cleanup_package /app/terraform/free_cleanup_package
COPY infrastructure/terraform/common_user_manager_package /app/terraform/common_user_manager_package
COPY main.py alembic.ini ./

ENV OPTINIST_DIR=/app/studio_data

# Copy the cloud-startup.sh script
COPY cloud-startup.sh ./
RUN chmod +x cloud-startup.sh

# Build metadata — placed last so the changing ARG values only
# invalidate this layer, not the COPY layers above.
ARG GIT_COMMIT=unknown
# GIT_BRANCH is empty when the build ran from a detached HEAD (a tag checkout);
# GIT_TAG is empty when HEAD carries no tag. Exactly one of the two identifies
# the build in the usual case — see infrastructure/scripts/git_ref_info.sh.
ARG GIT_BRANCH=unknown
ARG GIT_TAG=unknown
ARG BUILD_TIMESTAMP=unknown

LABEL org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.version="${GIT_TAG}" \
      org.opencontainers.image.created="${BUILD_TIMESTAMP}" \
      org.opencontainers.image.source="https://github.com/arayabrain/araya-optinist"

RUN python3 -c "import json,os,sys; json.dump({k.lower():os.environ.get(k,'unknown') for k in ('GIT_COMMIT','GIT_BRANCH','GIT_TAG','BUILD_TIMESTAMP')}, sys.stdout)" > /app/BUILD_INFO
