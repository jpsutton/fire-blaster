cmake_minimum_required(VERSION 3.13)

# Pico 2 by default; pass -DPICO_BOARD=pico for the original Pico.
if (NOT DEFINED PICO_BOARD)
    set(PICO_BOARD pico2)
endif()

include(pico_sdk_import.cmake)

project(fire_blaster_pico C CXX ASM)
set(CMAKE_C_STANDARD 11)

pico_sdk_init()

add_executable(fire_blaster_pico
    src/main.c
    src/irtoy.c
    src/ir_tx.c
    src/usb_descriptors.c
)
pico_generate_pio_header(fire_blaster_pico ${CMAKE_CURRENT_LIST_DIR}/src/ir_tx.pio)
target_include_directories(fire_blaster_pico PRIVATE src)
target_compile_options(fire_blaster_pico PRIVATE -Wall -Wextra)

set(IR_TX_PIN 2 CACHE STRING "GPIO driving the IR LED")
target_compile_definitions(fire_blaster_pico PRIVATE IR_TX_PIN=${IR_TX_PIN})

target_link_libraries(fire_blaster_pico
    pico_stdlib
    pico_unique_id
    hardware_pio
    tinyusb_device
)
pico_enable_stdio_usb(fire_blaster_pico 0)
pico_enable_stdio_uart(fire_blaster_pico 0)
pico_add_extra_outputs(fire_blaster_pico)
