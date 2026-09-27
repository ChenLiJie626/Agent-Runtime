cmake_minimum_required(VERSION 3.16)
project(use_after_free_multiclass LANGUAGES CXX)

add_executable(use_after_free_multiclass src/main.cpp src/workflow.cpp)
target_include_directories(use_after_free_multiclass PRIVATE include)
target_compile_features(use_after_free_multiclass PRIVATE cxx_std_17)

