// adapt_fpga.v — FPGA (Verilog) 适配示例
// 功能：每 100ms 通过 UART 发送一帧遥测数据
// 参数：CLK_FREQ=50MHz, BAUD=115200
// 接口：clk, rst_n, tx（接 RS-485 驱动芯片 DE/RE 控制自行添加）

module adapt_fpga #(
    parameter CLK_FREQ = 50_000_000,
    parameter BAUD     = 115_200
)(
    input  wire clk,
    input  wire rst_n,
    output reg  tx
);

// -------- UART 发送器 --------
localparam BAUD_DIV = CLK_FREQ / BAUD;  // 434

reg [8:0]  uart_data;   // {stop, data[7:0]}
reg [9:0]  uart_shift;
reg [9:0]  uart_baud_cnt;
reg [3:0]  uart_bit_cnt;
reg        uart_busy;

task uart_send_byte;
    input [7:0] d;
    begin
        uart_shift   <= {1'b1, d, 1'b0};  // stop + data + start
        uart_bit_cnt <= 0;
        uart_baud_cnt<= 0;
        uart_busy    <= 1;
    end
endtask

always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        tx <= 1; uart_busy <= 0;
    end else if (uart_busy) begin
        if (uart_baud_cnt < BAUD_DIV - 1) begin
            uart_baud_cnt <= uart_baud_cnt + 1;
        end else begin
            uart_baud_cnt <= 0;
            tx <= uart_shift[0];
            uart_shift <= {1'b1, uart_shift[9:1]};
            if (uart_bit_cnt == 9) uart_busy <= 0;
            else uart_bit_cnt <= uart_bit_cnt + 1;
        end
    end else begin
        tx <= 1;
    end
end

// -------- 帧数据（固定示例：转速1500rpm，温度25°C） --------
// 帧: AA 01 0E [14字节payload] [checksum] 55
// payload小端: speed=1500(0x05DC), spd_tgt=1500, cur=500(0x01F4),
//              ang_raw=0, ang=0, temp=65(25+40), quality=255, conv=255, flags=0, pad=0
// checksum = (0xDC+0x05+0xDC+0x05+0xF4+0x01+0x00+0x00+0x00+0x00+0x41+0xFF+0xFF+0x00) & 0xFF
localparam FRAME_LEN = 19;
reg [7:0] frame [0:FRAME_LEN-1];
initial begin
    frame[0]  = 8'hAA;  // 帧头
    frame[1]  = 8'h01;  // CMD_TELEMETRY
    frame[2]  = 8'h0E;  // payload长度14
    frame[3]  = 8'hDC;  // speed_actual低字节 (1500=0x05DC)
    frame[4]  = 8'h05;  // speed_actual高字节
    frame[5]  = 8'hDC;  // speed_target低字节
    frame[6]  = 8'h05;  // speed_target高字节
    frame[7]  = 8'hF4;  // current_actual低字节 (500mA=0x01F4)
    frame[8]  = 8'h01;  // current_actual高字节
    frame[9]  = 8'h00;  // angle_raw低字节
    frame[10] = 8'h00;  // angle_raw高字节
    frame[11] = 8'h00;  // angle_actual低字节
    frame[12] = 8'h00;  // angle_actual高字节
    frame[13] = 8'h41;  // temperature (25+40=65=0x41)
    frame[14] = 8'hFF;  // sensor_quality
    frame[15] = 8'hFF;  // convergence
    frame[16] = 8'h00;  // flags
    frame[17] = 8'hD4;  // checksum
    frame[18] = 8'h55;  // 帧尾
end

// -------- 100ms 定时发送 --------
localparam PERIOD = CLK_FREQ / 10;  // 100ms

reg [26:0] period_cnt;
reg [4:0]  byte_idx;
reg        sending;

always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        period_cnt <= 0; byte_idx <= 0; sending <= 0;
    end else begin
        if (!sending) begin
            if (period_cnt < PERIOD - 1) period_cnt <= period_cnt + 1;
            else begin period_cnt <= 0; sending <= 1; byte_idx <= 0; end
        end else if (!uart_busy) begin
            if (byte_idx < FRAME_LEN) begin
                uart_send_byte(frame[byte_idx]);
                byte_idx <= byte_idx + 1;
            end else begin
                sending <= 0;
            end
        end
    end
end

endmodule
