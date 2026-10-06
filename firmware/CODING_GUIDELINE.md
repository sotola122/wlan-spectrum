# C and C++ Coding Guidelines for Embedded Systems

These guidelines define a reusable baseline for readable, predictable, and portable embedded software. They apply to application code, device drivers, hardware adapters, and shared libraries, regardless of the MCU, SDK, RTOS, or build system.

**Must** states a requirement. **Should** states a default that may be changed for a concrete project need. **May** states an option. Record project-specific choices in the existing build or architecture documentation; do not create a separate process for routine decisions.

Examples use C11 and C++17. Each project must select its language versions explicitly and use only features supported by its toolchain. Vendor and generated code may retain their original conventions.

## 1. Core Principles

### 1.1 Keep the design simple

- Implement current requirements. Reuse suitable standard-library and existing project facilities before adding helpers, dependencies, or abstractions.
- Add an interface when it separates an actual hardware dependency, supports another implementation, or enables a useful test. Do not create an interface, factory, or wrapper for every class.
- Keep functions focused and dependencies explicit. Prefer composition to inheritance except where an interface requires polymorphism.
- Use private members for class invariants. Plain data structures may have public fields. Add getters and setters only when callers need them.

### 1.2 Do not use exceptions in firmware

- Firmware must not use `throw`, `try`, or `catch`. Configure exception support consistently with the toolchain and linked libraries.
- Report recoverable failures through return values. Do not call an operation whose failure handling requires an exception unless that failure is excluded by a checked precondition.
- Constructors should establish a valid local state. Use an explicit initialization function for hardware setup that can fail.
- Use RAII for deterministic cleanup of locks and other resources. RAII does not require exceptions or heap allocation.

### 1.3 Bound memory use and execution time

- General-purpose heap allocation and deallocation must be limited to initialization by default. This includes indirect use through containers, strings, callbacks, SDKs, and RTOS services, as well as `new`, `delete`, `malloc`, `realloc`, and `free`.
- Define where initialization ends. A project that needs runtime allocation must document the affected component, memory bound, timing requirements, and exhaustion behavior.
- Prefer fixed-capacity storage and objects with clear lifetimes. Account for stack limits; a large automatic buffer is not a substitute for a memory budget.
- Preallocated pools may be used when their capacity, operation cost, and exhaustion behavior are bounded.
- Hardware waits and retries must have a defined bound or cancellation path. Avoid unbounded recursion and variable-length arrays.

## 2. Naming Conventions

Use descriptive English names. Add units where they disambiguate a value, such as `timeout_ms`, `voltage_mv`, and `length_bytes`.

| Target | Convention | Example |
|---|---|---|
| Directories and source files | `snake_case` | `temperature_sensor.cpp`, `i2c_port.hpp` |
| C++ namespaces | `snake_case` | `hal`, `protocols` |
| Classes, structs, enums, and type aliases | `PascalCase` | `TemperatureSensor`, `SensorData`, `BusStatus` |
| Abstract interface classes | `I` + `PascalCase` | `II2cPort`, `ITemperatureSensor` |
| C++ functions and methods, including private methods | `camelCase` | `readTemperature()`, `calculateChecksum()` |
| C functions | `snake_case`; module prefix for exported functions | `frame_validate()`, `calculate_checksum()` |
| Parameters, local variables, and public data fields | `snake_case` | `device_address`, `retry_count` |
| Private C++ data members | `snake_case_` | `i2c_port_`, `initialized_` |
| File-local variables | `snake_case` | `rx_buffer`, `current_index` |
| Externally visible mutable globals | `g_` + module-qualified `snake_case` | `g_system_state` |
| Named constants | `UPPER_SNAKE_CASE` | `MAX_RETRY_COUNT`, `DEFAULT_TIMEOUT_MS` |
| C++ scoped enumerators | `UPPER_SNAKE_CASE` | `BusStatus::TIMEOUT` |
| C enumerators | Module/type prefix + `UPPER_SNAKE_CASE` | `FRAME_STATUS_INVALID_ARGUMENT` |
| Macros | Module prefix + `UPPER_SNAKE_CASE` | `DRIVER_LOG_ERROR` |
| Header guards | Unique path-based `UPPER_SNAKE_CASE` | `HAL_I2C_PORT_HPP`, `PROTOCOLS_FRAME_H` |

Constant naming takes precedence over variable naming, including for class constants. Prefer predicates such as `isReady()` and `hasData()` for Boolean queries. Use verbs for operations.

Do not introduce identifiers beginning with an underscore or containing consecutive underscores. This deliberately simple rule avoids reserved-identifier cases in C and C++; a lowercase letter after the leading underscore does not make a C file-scope name safe. Use implementation-provided identifiers such as `__cplusplus` only as documented, without redefining them. See [References](#references).

In C, `static` provides internal linkage for file-local functions and objects; their spelling does not. In C++, use an unnamed namespace or file-scope `static` for implementation-local entities. Avoid mutable globals where ownership can be expressed through an object or context parameter.

## 3. Files, Headers, and Formatting

### 3.1 File names

Use `.hpp` / `.cpp` for C++ and `.h` / `.c` for C. A header intended for both languages must expose a C-compatible API and use `.h`.

Source files must not require a project- or organization-specific prefix. Distinguish modules through directories and qualified include paths, such as `hal/i2c_port.hpp` and `drivers/temperature_sensor.hpp`. Use the same base name for a header and its implementation when practical.

### 3.2 Header rules

- Every header must compile when included on its own. Include the headers that declare the types it uses; do not rely on transitive includes.
- Use `#ifndef`, `#define`, and `#endif` guards. Do not use `#pragma once` under this guideline.
- Derive guards from the module-relative path and extension, ending in `_H` or `_HPP`. Add a library qualifier only where needed to keep guards unique across the combined build.
- Keep non-inline function definitions and externally linked mutable object definitions in source files. Put only the necessary declarations in public headers.
- Include the matching header first in each implementation file, followed by project and platform headers, then standard headers.
- Do not put `using namespace` directives or unnamed namespaces in headers.
- C headers consumed by C++ must use `#ifdef __cplusplus` around an `extern "C"` block. The block does not make C++-only declarations valid C.

### 3.3 Formatting and comments

Use the project's formatter configuration. If none exists, use four spaces, no tabs, opening braces on the same line, and a 100-column target. Always use braces for conditional and loop bodies.

Comments should explain a contract, hardware constraint, or non-obvious decision. Document public API units, ownership, failure behavior, and concurrency restrictions where applicable. Avoid comments that merely repeat the code.

## 4. Error Handling and API Contracts

| Situation | Preferred result |
|---|---|
| Predicate or operation with only two meaningful outcomes | `bool` |
| Operation whose failure reason affects the caller | A module-specific status enum |
| Data retrieval that can fail | Status return plus an output reference/pointer |
| Optional data with no required failure explanation | `std::optional<T>`, when available |

Use `enum class` in C++. Include only meaningful statuses, such as `OK`, `INVALID_ARGUMENT`, `TIMEOUT`, or `IO_ERROR`. Keep uninitialized state distinct from a missing device when the caller needs that distinction.

- Every fallible API must define its success value and the meaning of each failure result.
- Define whether output arguments remain unchanged, contain partial data, or become invalid on failure. Prefer unchanged outputs when inexpensive; use a temporary value and commit it after success.
- Callers must handle errors, propagate them, or explicitly document why ignoring them is acceptable. Use `[[nodiscard]]` for C++ results that must be checked when the selected standard supports it.
- Buffer APIs must define pointer validity, capacity, length units, zero-length behavior, and partial-transfer behavior. Validate externally supplied lengths before indexing or copying.
- Use assertions for internal invariants. Do not use them as the only validation for external data or expected hardware failures.
- Define timeout and retry semantics. Report errors at the layer that can add useful context instead of logging the same failure at every layer.

## 5. Ownership, Types, and Concurrency

- Each resource must have a clear owner. Inject required borrowed dependencies by reference in C++; use pointers when absence or reseating is meaningful. A borrowed dependency must outlive its user.
- Resource-owning types must define their copy and move behavior. Delete operations that would duplicate ownership incorrectly; do not add custom special members to simple value types without a reason.
- Initialize variables before use. Use `const` for data that is not modified and `constexpr` for suitable C++ compile-time constants.
- Use `std::uint8_t`, `std::uint32_t`, and related `<cstdint>` types in C++, or `<stdint.h>` equivalents in C, where exact width matters and the target supports them. Use `std::size_t` / `size_t` for object sizes and indices.
- Check range before narrowing a value. Do not rely on signed overflow, implicit signed/unsigned conversions, or implementation-dependent plain `char` signedness.
- Encode and decode wire formats explicitly. Do not serialize a native struct or use a bit-field layout as a portable packet definition; padding, alignment, and byte order are not a protocol contract.
- Document whether an API is task-only, ISR-safe, reentrant, or requires caller serialization. Keep ISR work bounded and use only operations supported in that context.
- Protect shared mutable state with the target's synchronization facilities. `volatile` does not provide atomicity or inter-thread synchronization. Confirm that any atomic operation used in an ISR is suitable for that target.

## 6. Namespaces and Platform Boundaries

Use namespaces that describe actual responsibilities. The following names are examples, not mandatory layers:

| Responsibility | Example namespace | Example content |
|---|---|---|
| Hardware interfaces | `hal` | `II2cPort`, `IUartPort` |
| Device drivers | `drivers` | `TemperatureSensor` |
| OS integration | `rtos` | Synchronization and scheduling adapters |
| Protocol logic | `protocols` | Framing, encoding, and decoding |

Keep nesting shallow, usually one or two levels. A reusable library may add a root namespace to avoid collisions. A `detail` namespace indicates an unsupported implementation API; it does not enforce access restrictions.

Portable application logic, device drivers, and public portable interfaces must not depend on vendor SDK headers, native RTOS handles, or MCU-specific types. Keep those dependencies in platform adapters and startup code. Translate native types and errors at that boundary.

| Code location, for example | Platform-specific dependencies |
|---|---|
| `include/hal/`, `include/drivers/` | Not permitted in portable interfaces |
| `src/drivers/`, `src/protocols/` | Not permitted in portable logic |
| `src/platform/<target>/` | Permitted for hardware and SDK adapters |
| `src/rtos/<os>/` | Permitted for RTOS integration |
| Target-specific startup and composition code | Permitted to construct and connect concrete implementations |

An SDK-backed network client belongs in a platform adapter even if it implements a protocol interface. If a platform-specific public API is needed, label and locate it separately from portable headers. Select target implementations in the build system; keep conditional compilation out of portable logic where practical.

## 7. C++ Example: Interface and Borrowed Dependency

This example uses an existing hardware boundary. It demonstrates naming, explicit status handling, and an unchanged output on failure. It does not prescribe an interface for every device or a complete I2C API.

```cpp
// include/hal/i2c_port.hpp
#ifndef HAL_I2C_PORT_HPP
#define HAL_I2C_PORT_HPP

#include <cstddef>
#include <cstdint>

namespace hal {

enum class BusStatus {
    OK,
    INVALID_ARGUMENT,
    TIMEOUT,
    IO_ERROR
};

class II2cPort {
public:
    virtual ~II2cPort() = default;

    // Synchronous, task-only operation. The caller serializes shared access.
    // device_address is an unshifted 7-bit address; reg_address is one byte.
    // out_data must reference at least length writable bytes; length > 0.
    // Invalid address values, nullptr, or zero length return INVALID_ARGUMENT.
    // The adapter enforces a finite configured timeout for the whole operation.
    // OK means all bytes were read; on failure, the buffer may be modified.
    [[nodiscard]] virtual BusStatus readRegister(
        std::uint8_t device_address,
        std::uint8_t reg_address,
        std::uint8_t* out_data,
        std::size_t length) = 0;
};

} // namespace hal

#endif // HAL_I2C_PORT_HPP
```

```cpp
// include/drivers/register_device.hpp
#ifndef DRIVERS_REGISTER_DEVICE_HPP
#define DRIVERS_REGISTER_DEVICE_HPP

#include "hal/i2c_port.hpp"

#include <cstdint>

namespace drivers {

class RegisterDevice {
public:
    // The initialized port must outlive this object.
    RegisterDevice(hal::II2cPort& i2c_port, std::uint8_t device_address)
        : i2c_port_(i2c_port), device_address_(device_address) {}

    // Inherits the port's task and synchronization restrictions.
    // out_value remains unchanged unless the read succeeds.
    [[nodiscard]] hal::BusStatus readValue(
        std::uint8_t reg_address, std::uint8_t& out_value) {
        std::uint8_t value = 0;
        const auto status = i2c_port_.readRegister(
            device_address_, reg_address, &value, sizeof(value));
        if (status != hal::BusStatus::OK) {
            return status;
        }
        out_value = value;
        return hal::BusStatus::OK;
    }

private:
    hal::II2cPort& i2c_port_;
    std::uint8_t device_address_;
};

} // namespace drivers

#endif // DRIVERS_REGISTER_DEVICE_HPP
```

## 8. C Example: Explicit Frame Boundaries

The caller supplies one complete frame: at least one payload byte followed by one checksum byte. The checksum is the payload sum modulo 256. Delimiters and transport framing are outside this function's contract.

```c
// include/protocols/frame.h
#ifndef PROTOCOLS_FRAME_H
#define PROTOCOLS_FRAME_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    FRAME_STATUS_OK,
    FRAME_STATUS_INVALID_ARGUMENT,
    FRAME_STATUS_CHECKSUM_MISMATCH
} FrameStatus;

// frame must reference frame_length readable bytes.
// NULL and lengths below two are rejected. No data is modified.
FrameStatus frame_validate(const uint8_t* frame, size_t frame_length);

#ifdef __cplusplus
}
#endif

#endif // PROTOCOLS_FRAME_H
```

```c
// src/protocols/frame.c
#include "protocols/frame.h"

static uint8_t calculate_checksum(const uint8_t* data, size_t length) {
    uint8_t sum = 0;
    for (size_t i = 0; i < length; ++i) {
        sum = (uint8_t)(sum + data[i]);
    }
    return sum;
}

FrameStatus frame_validate(const uint8_t* frame, size_t frame_length) {
    if (frame == NULL || frame_length < 2) {
        return FRAME_STATUS_INVALID_ARGUMENT;
    }

    const size_t payload_length = frame_length - 1;
    if (calculate_checksum(frame, payload_length) != frame[payload_length]) {
        return FRAME_STATUS_CHECKSUM_MISMATCH;
    }
    return FRAME_STATUS_OK;
}
```

## 9. Tests and Test Doubles

Test observable behavior and meaningful failure paths. For embedded code, relevant cases include buffer boundaries, invalid frames, timeouts, initialization failures, and output validity after an error. Keep host tests independent of the target SDK where practical; verify hardware behavior and timing on the target.

| Target | Convention | Example |
|---|---|---|
| C++ test file | `test_<subject>.cpp` | `test_register_device.cpp` |
| C test file | `test_<subject>.c` | `test_frame.c` |
| Mock file and class | `mock_<role>.hpp`, `Mock<Role>` | `mock_i2c_port.hpp`, `MockI2cPort` |
| Fake file and class | `fake_<role>.hpp`, `Fake<Role>` | `fake_i2c_port.hpp`, `FakeI2cPort` |

Drop the interface's initial `I` when naming a test double. A fake provides a simplified working behavior; a mock verifies expected interactions. Use the simplest one needed, keeping it in the test file when it is not shared. Overrides must match the interface exactly, including namespace, parameter types, qualifiers, and return type, and must use `override`.

When using GoogleTest, use `<Subject>Test` for suites and descriptive `PascalCase` test names without underscores, such as `ReadValueWhenTimeoutLeavesOutputUnchanged`. This follows GoogleTest's naming guidance; framework-defined special prefixes are separate conventions. See [References](#references).

The following standalone host check exercises the C example without a test framework:

```c
// tests/test_frame.c
#include "protocols/frame.h"

#include <assert.h>

int main(void) {
    const uint8_t valid[] = {0xFF, 0x02, 0x01};
    const uint8_t invalid[] = {0xFF, 0x02, 0x00};
    const uint8_t minimal[] = {0x07, 0x07};
    const uint8_t incomplete[] = {0x07};

    assert(frame_validate(valid, sizeof(valid)) == FRAME_STATUS_OK);
    assert(frame_validate(minimal, sizeof(minimal)) == FRAME_STATUS_OK);
    assert(frame_validate(invalid, sizeof(invalid)) == FRAME_STATUS_CHECKSUM_MISMATCH);
    assert(frame_validate(NULL, 2) == FRAME_STATUS_INVALID_ARGUMENT);
    assert(frame_validate(valid, 0) == FRAME_STATUS_INVALID_ARGUMENT);
    assert(frame_validate(incomplete, sizeof(incomplete)) == FRAME_STATUS_INVALID_ARGUMENT);
    return 0;
}
```

Compile assertion-based checks with assertions enabled. Test scaffolding may use host facilities that are excluded from firmware, provided it is not linked into the firmware image.

## 10. Build and Documentation Consistency

- Keep the selected language standards, warning options, and formatter settings in the build configuration or existing project documentation.
- Fix new warnings in project-owned code. Scope necessary vendor-code suppressions to the relevant dependency.
- Compile affected targets and run tests that cover changed behavior. Use additional analysis or hardware checks where they address a concrete risk.
- Keep public contracts, code examples, include paths, and architecture documentation consistent with the implementation. If an `ARCHITECTURE.md` exists, update it when module responsibilities or platform boundaries change.
- Apply these conventions to new and modified code without unrelated mass renaming of otherwise untouched code.

## References

- [C++ working draft: identifiers](https://eel.is/c++draft/lex.name) — reserved identifier rules.
- [C11 committee draft N1570, section 7.1.3](https://www.open-std.org/jtc1/sc22/wg14/www/docs/n1570.pdf) — reserved identifiers, including underscore-prefixed file-scope names.
- [GoogleTest FAQ](https://google.github.io/googletest/faq.html#why-should-test-suite-names-and-test-names-not-contain-underscore) — test suite and test naming restrictions.

