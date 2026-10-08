// IR blaster on a Raspberry Pi Pico that enumerates as a USB IR Toy, so
// Linux's ir_toy driver presents it as a /dev/lircN transmitter.

#include "pico/stdlib.h"
#include "tusb.h"

#include "ir_tx.h"
#include "irtoy.h"

#ifndef IR_TX_PIN
#define IR_TX_PIN 2
#endif

static irtoy_t toy;

// Wait out the previous reply first, so each reply goes out as its own
// packet.
static void usb_send(void *ctx, const uint8_t *buf, size_t len)
{
    (void)ctx;
    while (tud_ready() && tud_cdc_write_available() < CFG_TUD_CDC_TX_BUFSIZE)
        tud_task();
    if (!tud_ready())
        return;
    tud_cdc_write(buf, len);
    tud_cdc_write_flush();
}

static void transmit(void *ctx, const uint16_t *units, size_t count, uint32_t carrier_hz)
{
    (void)ctx;
#ifdef PICO_DEFAULT_LED_PIN
    gpio_put(PICO_DEFAULT_LED_PIN, 1);
#endif
    ir_tx_begin(carrier_hz);
    for (size_t i = 0; i < count; i += 2) {
        // A write ends on a mark; pad it with the shortest space.
        uint32_t space = i + 1 < count ? units[i + 1] * IRTOY_UNIT_US : 0;
        ir_tx_put(units[i] * IRTOY_UNIT_US, space);
    }
    ir_tx_wait();
#ifdef PICO_DEFAULT_LED_PIN
    gpio_put(PICO_DEFAULT_LED_PIN, 0);
#endif
}

static const irtoy_io_t io = { .send = usb_send, .transmit = transmit };

void tud_mount_cb(void)
{
    irtoy_init(&toy, &io);
}

int main(void)
{
#ifdef PICO_DEFAULT_LED_PIN
    gpio_init(PICO_DEFAULT_LED_PIN);
    gpio_set_dir(PICO_DEFAULT_LED_PIN, GPIO_OUT);
#endif
    ir_tx_init(IR_TX_PIN);
    irtoy_init(&toy, &io);
    tud_init(0);

    for (;;) {
        tud_task();
        if (tud_cdc_available()) {
            uint8_t buf[64];
            uint32_t n = tud_cdc_read(buf, sizeof buf);
            irtoy_feed(&toy, buf, n);
        }
    }
}
