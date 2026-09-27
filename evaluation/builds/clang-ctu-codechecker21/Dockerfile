FROM agent-runtime/clang-ctu:clang18-codechecker6291

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gnupg \
        lsb-release \
        wget \
    && wget -qO /usr/share/keyrings/apt.llvm.org.asc \
        https://apt.llvm.org/llvm-snapshot.gpg.key \
    && echo 'deb [signed-by=/usr/share/keyrings/apt.llvm.org.asc] https://apt.llvm.org/noble/ llvm-toolchain-noble-21 main' \
        > /etc/apt/sources.list.d/llvm21.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends clang-21 clang-tools-21 \
    && rm -rf /var/lib/apt/lists/*

# libstdc++ 13 emits a file-scope assembler alias for iostream initialization.
# The alias has no effect on source analysis, but Clang 21's CTU AST importer
# cannot import FileScopeAsmDecl yet. Remove only that declaration in the
# analysis image; project sources and executable builds remain untouched.
RUN sed -i '/__extension__ __asm (".globl _ZSt21ios_base_library_initv");/d' \
    /usr/include/c++/13/iostream

ENV CC=clang CXX=clang++
ENV PATH=/opt/codechecker/bin:/usr/lib/llvm-21/bin:$PATH
WORKDIR /workspace
