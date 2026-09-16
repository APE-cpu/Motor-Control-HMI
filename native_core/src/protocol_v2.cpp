#include "motor_core/protocol_v2.hpp"

#include <algorithm>
#include <array>
#include <utility>

namespace motor_core {
namespace {

std::uint16_t read_u16_le(const std::uint8_t* data) noexcept {
    return static_cast<std::uint16_t>(data[0]) |
           (static_cast<std::uint16_t>(data[1]) << 8U);
}

bool valid_message_type(std::uint8_t value) noexcept {
    return value >= 1U && value <= 7U;
}

constexpr std::array<std::uint8_t, 2> kMagic{kMagic0, kMagic1};

}  // namespace

std::uint16_t crc16_ccitt(const std::uint8_t* data, std::size_t size,
                          std::uint16_t initial) noexcept {
    std::uint16_t crc = initial;
    for (std::size_t index = 0; index < size; ++index) {
        crc ^= static_cast<std::uint16_t>(data[index]) << 8U;
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x8000U) != 0U
                      ? static_cast<std::uint16_t>((crc << 1U) ^ 0x1021U)
                      : static_cast<std::uint16_t>(crc << 1U);
        }
    }
    return crc;
}

std::vector<Frame> StreamDecoder::feed(const std::uint8_t* data,
                                       std::size_t size) {
    if (size != 0U) {
        buffer_.insert(buffer_.end(), data, data + size);
    }

    std::vector<Frame> frames;
    while (true) {
        auto begin = buffer_.begin() + static_cast<std::ptrdiff_t>(head_);
        auto magic = std::search(begin, buffer_.end(), kMagic.begin(), kMagic.end());
        if (magic == buffer_.end()) {
            const bool keep_magic_prefix =
                !buffer_.empty() && buffer_.back() == kMagic0;
            if (keep_magic_prefix) {
                const auto last = buffer_.back();
                buffer_.assign(1, last);
            } else {
                buffer_.clear();
            }
            head_ = 0;
            break;
        }

        head_ = static_cast<std::size_t>(magic - buffer_.begin());
        const std::size_t available = buffer_.size() - head_;
        if (available < kHeaderSize) {
            break;
        }

        const auto* candidate = buffer_.data() + head_;
        const std::size_t payload_size = read_u16_le(candidate + 8U);
        if (payload_size > kMaxPayload) {
            ++error_count_;
            ++head_;
            continue;
        }

        const std::size_t total = kHeaderSize + payload_size + 3U;
        if (available < total) {
            break;
        }

        Frame frame;
        if (decode_at(head_, total, frame)) {
            frames.push_back(std::move(frame));
            head_ += total;
        } else {
            ++error_count_;
            ++head_;
        }
    }

    compact();
    return frames;
}

bool StreamDecoder::decode_at(std::size_t offset, std::size_t total,
                              Frame& frame) const {
    const auto* data = buffer_.data() + offset;
    if (total < kMinFrameSize || data[0] != kMagic0 || data[1] != kMagic1) {
        return false;
    }
    const std::size_t payload_size = read_u16_le(data + 8U);
    if (payload_size > kMaxPayload || total != kHeaderSize + payload_size + 3U) {
        return false;
    }
    if (data[total - 1U] != kTail || !valid_message_type(data[6])) {
        return false;
    }
    const auto expected_crc = read_u16_le(data + kHeaderSize + payload_size);
    const auto actual_crc = crc16_ccitt(data + 2U, 8U + payload_size);
    if (expected_crc != actual_crc) {
        return false;
    }

    frame.version = data[2];
    frame.address = data[3];
    frame.sequence = read_u16_le(data + 4U);
    frame.message_type = data[6];
    frame.command = data[7];
    frame.payload.assign(data + kHeaderSize,
                         data + kHeaderSize + payload_size);
    return true;
}

void StreamDecoder::compact() {
    if (head_ == 0U) {
        return;
    }
    if (head_ >= buffer_.size()) {
        buffer_.clear();
    } else {
        buffer_.erase(buffer_.begin(),
                      buffer_.begin() + static_cast<std::ptrdiff_t>(head_));
    }
    head_ = 0;
}

void StreamDecoder::reset() noexcept {
    buffer_.clear();
    head_ = 0;
    error_count_ = 0;
}

}  // namespace motor_core
