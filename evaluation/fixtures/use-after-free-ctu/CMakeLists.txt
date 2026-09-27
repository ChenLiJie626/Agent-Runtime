cmake_minimum_required(VERSION 3.16)
project(use_after_free_ctu LANGUAGES CXX)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
add_executable(use_after_free_ctu
    src/main.cpp
    src/heap_owner.cpp
    src/borrowed_view.cpp
    src/workflow.cpp)
target_include_directories(use_after_free_ctu PRIVATE include)
target_compile_features(use_after_free_ctu PRIVATE cxx_std_17)

