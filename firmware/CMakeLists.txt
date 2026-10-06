cmake_minimum_required(VERSION 3.16)
# Emit build/compile_commands.json so the host LSP can resolve IDF headers.
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(wifi_monitor)
# Version gate: tools/cmake/version.cmake exports IDF_VERSION into the
# environment while project() runs, so the check must follow project().
if(NOT "$ENV{IDF_VERSION}" STREQUAL "6.0.3")
    message(FATAL_ERROR "ESP-IDF 6.0.3 is required; found $ENV{IDF_VERSION}")
endif()
if(NOT IDF_TARGET STREQUAL "esp32c5")
    message(FATAL_ERROR "Only esp32c5 is supported, found ${IDF_TARGET}")
endif()
