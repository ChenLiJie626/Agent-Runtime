cmake_minimum_required(VERSION 3.16)
project(use_after_free_simple LANGUAGES CXX)

add_executable(use_after_free_simple src/main.cpp)
target_compile_features(use_after_free_simple PRIVATE cxx_std_17)

