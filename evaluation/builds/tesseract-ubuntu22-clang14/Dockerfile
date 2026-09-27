FROM docker.m.daocloud.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates=20260601~22.04.1 \
        clang-14=1:14.0.0-1ubuntu1.1 \
        cmake=3.22.1-1ubuntu1.22.04.2 \
        libarchive-dev=3.6.0-1ubuntu1.8 \
        libcurl4-openssl-dev=7.81.0-1ubuntu1.29 \
        libleptonica-dev=1.82.0-3build1 \
        make=4.3-4.1build1 \
        ninja-build=1.10.1-1 \
        pkg-config=0.29.2-1ubuntu3 \
    && rm -rf /var/lib/apt/lists/*

ENV CC=clang-14 CXX=clang++-14
WORKDIR /workspace
