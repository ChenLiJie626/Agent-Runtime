FROM docker.m.daocloud.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02

ENV DEBIAN_FRONTEND=noninteractive

COPY packages.lock requirements.lock /tmp/agent-runtime-locks/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
        clang-14 \
        clang-tools-14 \
        cmake \
        make \
        ninja-build \
        python3 \
        python3-dev \
        python3-venv \
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
    && dpkg-query -W -f='${Package}\t${Version}\n' | sort \
        > /tmp/agent-runtime-locks/actual-packages.lock \
    && cmp /tmp/agent-runtime-locks/packages.lock \
        /tmp/agent-runtime-locks/actual-packages.lock \
    && rm -rf /var/lib/apt/lists/*

ENV CC=clang-14 CXX=clang++-14
ENV PATH=/opt/codechecker/bin:/usr/lib/llvm-14/bin:$PATH
WORKDIR /workspace
