FROM --platform=linux/amd64 python:3.9-slim

WORKDIR /app

# install default packages
RUN apt-get --allow-releaseinfo-change update && \
    apt-get install --no-install-recommends -y sudo procps iproute2 iputils-ping telnet wget curl unzip less vim \
        default-mysql-client

# install conda & packages
RUN apt-get install --no-install-recommends -y gcc g++ libgl1 libgl1-mesa-dev libopencv-dev && \
    apt-get autoremove -y && apt-get clean
RUN mkdir -p /opt/miniforge && \
    curl -L "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh" -o /opt/miniforge/Miniforge3.sh && \
    bash /opt/miniforge/Miniforge3.sh -b -u -p /opt/miniforge && \
    rm /opt/miniforge/Miniforge3.sh && \
    export PATH="$PATH:/opt/miniforge/bin" && \
    conda upgrade -y --all && \
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
COPY main.py alembic.ini ./

# Copy the cloud-startup.sh script
COPY cloud-startup.sh ./
RUN chmod +x cloud-startup.sh
