FROM --platform=linux/amd64 python:3.8.18-slim

WORKDIR /app

# install default packages
RUN apt-get --allow-releaseinfo-change update && \
    apt-get install --no-install-recommends -y sudo procps iproute2 iputils-ping wget unzip less vim

# install conda & packages
RUN mkdir -p /opt/miniconda3 && \
    apt-get install --no-install-recommends -y gcc g++ libgl1 libgl1-mesa-dev libopencv-dev curl default-mysql-server && \
    wget -q https://repo.anaconda.com/miniconda/Miniconda3-py39_4.12.0-Linux-x86_64.sh -O /opt/miniconda3/miniconda.sh && \
    bash /opt/miniconda3/miniconda.sh -b -u -p /opt/miniconda3 && \
    rm /opt/miniconda3/miniconda.sh && \
    export PATH="$PATH:/opt/miniconda3/bin" && \
    conda upgrade -y --all && \
    conda config --set channel_priority strict && \
    conda clean -y --tarballs
ENV PATH $PATH:/opt/miniconda3/bin

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

# copy optinist files
COPY frontend/build /app/frontend/build
COPY studio /app/studio
COPY sample_data /app/sample_data
COPY main.py alembic.ini ./

# Copy the cloud-startup.sh script
COPY studio/app/cloud-startup.sh ./
RUN chmod +x cloud-startup.sh

EXPOSE 8000

# Set the entrypoint to run cloud-startup.sh
ENTRYPOINT ["./cloud-startup.sh"]
