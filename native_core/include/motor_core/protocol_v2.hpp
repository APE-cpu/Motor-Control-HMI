#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace motor_core {

constexpr std::uint8_t kMagic0 = 0xA5;
constexpr std::uint8_t kMagic1 = 0x5A;
constexpr std::uint8_t kTail = 0x7E;
constexpr std::size_t kHeaderSize = 10;
constexpr std::size_t kMaxPayload = 4096;
constexpr std::size_t kMinFrameSize = kHeaderSize + 3;

struct Frame {
    std::uint8_t version = 2;
    std::uint8_t address = 1;
    std::uint16_t sequence = 0;
    std::uint8_t message_type = 0;
    std::uint8_t command = 0;
    std::vector<std::uint8_t> payload;
};

std::uint16_t crc16_ccitt(const std::uint8_t* data, std::size_t size,
                          std::uint16_t initial = 0xFFFF) noexcept;

class StreamDecoder {
public:
    std::vector<Frame> feed(const std::uint8_t* data, std::size_t size);

    std::uint64_t error_count() const noexcept { return error_count_; }
    std::size_t buffered_bytes() const noexcept { return buffer_.size() - head_; }
    void reset() noexcept;

private:
    bool decode_at(std::size_t offset, std::size_t total, Frame& frame) const;
    void compact();

    std::vector<std::uint8_t> buffer_;
    std::size_t head_ = 0;
    std::uint64_t error_count_ = 0;
};

}  // namespace motor_core
