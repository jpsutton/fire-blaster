#include "ir_tx.h"

#include "hardware/clocks.h"
#include "hardware/gpio.h"
#include "hardware/pio.h"
#include "ir_tx.pio.h"

static PIO pio;
static uint sm;
static uint32_t carrier;

void ir_tx_init(unsigned pin)
{
    uint offset;
    hard_assert(pio_claim_free_sm_and_add_program_for_gpio_range(&ir_tx_program, &pio, &sm, &offset, pin, 1, true));

    pio_sm_config c = ir_tx_program_get_default_config(offset);
    sm_config_set_sideset_pins(&c, pin);
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_TX);
    sm_config_set_out_shift(&c, true, false, 32);

    pio_sm_set_pins_with_mask(pio, sm, 0, 1u << pin);
    pio_sm_set_consecutive_pindirs(pio, sm, pin, 1, true);
    pio_gpio_init(pio, pin);
    // The pin feeds the LED directly through 220 ohms, so drive it hard.
    gpio_set_drive_strength(pin, GPIO_DRIVE_STRENGTH_12MA);

    pio_sm_init(pio, sm, offset, &c);
    ir_tx_begin(38000);
    pio_sm_set_enabled(pio, sm, true);
}

void ir_tx_begin(uint32_t carrier_hz)
{
    carrier = carrier_hz;
    pio_sm_set_clkdiv(pio, sm, (float)clock_get_hz(clk_sys) / ((float)carrier_hz * ir_tx_CYCLES_PER_PERIOD));
}

// Whole carrier periods, rounded, at least one; the program counts down
// from n - 1.
static uint32_t periods(uint32_t us)
{
    uint64_t n = ((uint64_t)us * carrier + 500000) / 1000000;
    return n ? (uint32_t)n - 1 : 0;
}

void ir_tx_put(uint32_t mark_us, uint32_t space_us)
{
    pio_sm_put_blocking(pio, sm, periods(mark_us));
    pio_sm_put_blocking(pio, sm, periods(space_us));
}

void ir_tx_wait(void)
{
    while (!pio_sm_is_tx_fifo_empty(pio, sm))
        tight_loop_contents();
    // The last pair is pulled; the program stalls on the next pull once
    // its space is over.
    uint32_t stall = 1u << (PIO_FDEBUG_TXSTALL_LSB + sm);
    pio->fdebug = stall;
    while (!(pio->fdebug & stall))
        tight_loop_contents();
}
