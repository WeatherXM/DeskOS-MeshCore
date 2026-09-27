cmake_minimum_required(VERSION 3.16)

set(D1L_RELEASE_PROFILE "core_1_0" CACHE STRING
    "Immutable DeskOS release profile")
set_property(CACHE D1L_RELEASE_PROFILE PROPERTY STRINGS
    development
    core_1_0
    full_feature
)
if(NOT D1L_RELEASE_PROFILE STREQUAL "development" AND
   NOT D1L_RELEASE_PROFILE STREQUAL "core_1_0" AND
   NOT D1L_RELEASE_PROFILE STREQUAL "full_feature")
    message(FATAL_ERROR
        "D1L_RELEASE_PROFILE must be development, core_1_0, or full_feature")
endif()

set(D1L_SD_HISTORY_MODE "conditional" CACHE STRING
    "Immutable release-package SD history claim")
set_property(CACHE D1L_SD_HISTORY_MODE PROPERTY STRINGS
    disabled
    conditional
    supported_optional
)
if(NOT D1L_SD_HISTORY_MODE STREQUAL "disabled" AND
   NOT D1L_SD_HISTORY_MODE STREQUAL "conditional" AND
   NOT D1L_SD_HISTORY_MODE STREQUAL "supported_optional")
    message(FATAL_ERROR
        "D1L_SD_HISTORY_MODE must be disabled, conditional, or supported_optional")
endif()

# Resolve provenance before applying the reviewed BSP patches below.  This
# keeps the release-cleanliness check bound to the exact source checkout while
# allowing the deterministic submodule patch step to proceed afterwards.
include("${CMAKE_CURRENT_LIST_DIR}/cmake/d1l_source_provenance.cmake")
d1l_resolve_source_provenance(
    "${CMAKE_CURRENT_LIST_DIR}"
    D1L_BUILD_GIT_COMMIT
    D1L_BUILD_EPOCH_SEC
)
file(SHA256 "${CMAKE_CURRENT_LIST_DIR}/partitions_d1l.csv"
     D1L_PARTITION_TABLE_SHA256)

if(NOT DEFINED PROJECT_VER)
    execute_process(
        COMMAND git rev-parse --short HEAD
        WORKING_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}"
        OUTPUT_VARIABLE D1L_GIT_SHORT_SHA
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
    )
    if(D1L_GIT_SHORT_SHA)
        set(PROJECT_VER "${D1L_GIT_SHORT_SHA}")
    endif()
endif()

# Vendored BSP and hardware components are located in components/


include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(meshcore_deskos_d1l)

foreach(seeed_component IN ITEMS
    bsp
    bus
    i2c_devices
    iot_button
    lora
    lvgl
)
    idf_component_get_property(seeed_component_lib ${seeed_component} COMPONENT_LIB)
    target_compile_options(${seeed_component_lib} PRIVATE
        "-Wno-error=format"
        "-Wno-error=maybe-uninitialized"
    )
endforeach()
