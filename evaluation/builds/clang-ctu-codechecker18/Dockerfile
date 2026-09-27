FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

COPY evaluation/builds/clang-ctu-codechecker/requirements.lock /tmp/agent-runtime-locks/requirements.lock

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bison \
        build-essential \
        ca-certificates \
        clang \
        clang-tools \
        cmake \
        flex \
        git \
        libboost-container1.83-dev \
        libboost-iostreams1.83-dev \
        libboost-program-options1.83-dev \
        libboost-url1.83-dev \
        libicu-dev \
        libjemalloc-dev \
        libssl-dev \
        libzstd-dev \
        make \
        ninja-build \
        pkg-config \
        python3 \
        python3-dev \
        python3-venv \
        uuid-dev \
    && python3 -m venv /opt/codechecker \
    && /opt/codechecker/bin/pip install --no-cache-dir --no-deps codechecker==6.29.1 \
    && /opt/codechecker/bin/pip install --no-cache-dir --no-deps sarif-tools==3.0.5 \
    && /opt/codechecker/bin/pip install --no-cache-dir --no-deps jsonpath-ng==1.8.0 \
    && /opt/codechecker/bin/pip install --no-cache-dir \
        --constraint /tmp/agent-runtime-locks/requirements.lock \
        alembic==1.20.0 \
        argcomplete==3.7.2 \
        Authlib==1.8.0 \
        gitpython==3.1.62 \
        lxml==6.1.3 \
        multiprocess==0.70.19 \
        portalocker==3.2.0 \
        prettytable==3.18.0 \
        psutil==7.2.2 \
        PyYAML==6.0.3 \
        requests==2.34.2 \
        semver==3.1.0 \
        sqlalchemy==2.0.54 \
        thrift==0.24.0 \
    && rm -rf /var/lib/apt/lists/*

ENV CC=clang CXX=clang++
ENV PATH=/opt/codechecker/bin:/usr/lib/llvm-18/bin:$PATH
WORKDIR /workspace
