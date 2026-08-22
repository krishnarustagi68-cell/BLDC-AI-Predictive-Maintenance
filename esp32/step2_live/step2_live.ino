// STAGE 1 - run this FIRST.
// Confirms the MPU9250 actually shows up on I2C0 (GPIO0=SDA, GPIO1=SCL)
// before you trust anything built on top of it.
#include "pico/stdlib.h"
#include "hardware/i2c.h"
#include <stdio.h>

int main() {
    stdio_init_all();
    sleep_ms(2000);

    i2c_init(i2c0, 400 * 1000);
    gpio_set_function(0, GPIO_FUNC_I2C); // SDA
    gpio_set_function(1, GPIO_FUNC_I2C); // SCL
    gpio_pull_up(0);
    gpio_pull_up(1);

    printf("Scanning I2C0 (GPIO0/1)...\n");
    int found = 0;
    for (int addr = 0x08; addr < 0x78; addr++) {
        uint8_t rxdata;
        int ret = i2c_read_blocking(i2c0, addr, &rxdata, 1, false);
        if (ret >= 0) {
            printf("  Found device at 0x%02X\n", addr);
            found++;
        }
    }
    if (found == 0) {
        printf("NOTHING FOUND. Check wiring, pull-ups, and 3.3V power to the module.\n");
    } else {
        printf("Done. MPU9250 should show up at 0x68 (or 0x69 if AD0 is high).\n");
    }

    while (1) sleep_ms(1000);
}
