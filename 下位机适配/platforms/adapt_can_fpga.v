// adapt_can_fpga.v — FPGA CAN 控制器适配（通过 SPI 驱动 MCP2515）
// 功能：每 100ms 通过 MCP2515 发送一帧 8 字节 CAN 遥测报文
// CAN ID: 0x100，波特率 500kbps，晶振 16MHz
// 接口：clk(50MHz), rst_n, spi_sck, spi_mosi, spi_miso, spi_cs_n

module adapt_can_fpga #(
    parameter CLK_FREQ = 50_000_000
)(
    input  wire clk,
    input  wire rst_n,
    output reg  spi_sck,
    output reg  spi_mosi,
    input  wire spi_miso,
    output reg  spi_cs_n
);

// MCP2515 SPI 指令
localparam MCP_RESET    = 8'hC0;
localparam MCP_WRITE    = 8'h02;
localparam MCP_RTS_TX0  = 8'h81;  // 触发 TXB0 发送

// MCP2515 寄存器地址
localparam TXB0SIDH = 8'h31;  // 发送缓冲区 ID 高字节
localparam TXB0SIDL = 8'h32;
localparam TXB0DLC  = 8'h35;
localparam TXB0D0   = 8'h36;  // 数据字节 0-7

// 遥测数据（示例固定值：转速1500rpm，电流500mA）
// speed=1500(0x05DC), spd_tgt=1500, cur=500(0x01F4), ang_raw=0
localparam [63:0] TELEM_DATA = 64'h0000_01F4_05DC_05DC;

// SPI 时钟分频（50MHz / 100 = 500kHz SPI）
localparam SPI_DIV = 50;

reg [7:0]  spi_tx_byte;
reg [2:0]  spi_bit_cnt;
reg [6:0]  spi_clk_cnt;
reg        spi_busy;
reg        spi_done;

// SPI 发送一个字节
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        spi_sck <= 0; spi_mosi <= 0; spi_busy <= 0; spi_done <= 0;
    end else if (spi_busy) begin
        if (spi_clk_cnt < SPI_DIV/2 - 1) begin
            spi_clk_cnt <= spi_clk_cnt + 1;
        end else begin
            spi_clk_cnt <= 0;
            spi_sck <= ~spi_sck;
            if (spi_sck) begin  // 下降沿移出数据
                spi_mosi <= spi_tx_byte[7];
                spi_tx_byte <= {spi_tx_byte[6:0], 1'b0};
                if (spi_bit_cnt == 7) begin
                    spi_busy <= 0; spi_done <= 1;
                end else spi_bit_cnt <= spi_bit_cnt + 1;
            end
        end
    end else spi_done <= 0;
end

// 初始化 + 周期发送状态机
localparam PERIOD = CLK_FREQ / 10;  // 100ms
reg [26:0] period_cnt;
reg [5:0]  state;
reg [7:0]  cmd_buf [0:15];
reg [3:0]  cmd_len, cmd_idx;

localparam S_RESET=0, S_WAIT_RESET=1, S_INIT=2, S_IDLE=3,
           S_SEND_CS_LOW=4, S_SEND_BYTE=5, S_SEND_CS_HIGH=6, S_DONE=7;

always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        state <= S_RESET; spi_cs_n <= 1; period_cnt <= 0;
    end else case (state)
        S_RESET: begin
            // 发送 RESET 指令
            spi_cs_n <= 0;
            spi_tx_byte <= MCP_RESET; spi_bit_cnt <= 0;
            spi_clk_cnt <= 0; spi_busy <= 1;
            state <= S_WAIT_RESET;
        end
        S_WAIT_RESET: if (spi_done) begin spi_cs_n <= 1; state <= S_INIT; end
        S_INIT: begin
            // 简化：跳过详细初始化，假设 MCP2515 已配置好 500kbps
            // 实际使用时需通过 SPI 写 CNF1/CNF2/CNF3 寄存器配置波特率
            state <= S_IDLE;
        end
        S_IDLE: begin
            if (period_cnt < PERIOD - 1) period_cnt <= period_cnt + 1;
            else begin
                period_cnt <= 0;
                // 准备写 TXB0：ID=0x100, DLC=8, DATA=TELEM_DATA
                cmd_buf[0]  <= MCP_WRITE;
                cmd_buf[1]  <= TXB0SIDH;
                cmd_buf[2]  <= 8'h20;  // SIDH: 0x100 >> 3 = 0x20
                cmd_buf[3]  <= 8'h00;  // SIDL: (0x100 & 0x7) << 5 = 0x00
                cmd_buf[4]  <= 8'h00;  // EID8
                cmd_buf[5]  <= 8'h00;  // EID0
                cmd_buf[6]  <= 8'h08;  // DLC = 8
                cmd_buf[7]  <= TELEM_DATA[7:0];
                cmd_buf[8]  <= TELEM_DATA[15:8];
                cmd_buf[9]  <= TELEM_DATA[23:16];
                cmd_buf[10] <= TELEM_DATA[31:24];
                cmd_buf[11] <= TELEM_DATA[39:32];
                cmd_buf[12] <= TELEM_DATA[47:40];
                cmd_buf[13] <= TELEM_DATA[55:48];
                cmd_buf[14] <= TELEM_DATA[63:56];
                cmd_len <= 15; cmd_idx <= 0;
                state <= S_SEND_CS_LOW;
            end
        end
        S_SEND_CS_LOW: begin spi_cs_n <= 0; state <= S_SEND_BYTE; end
        S_SEND_BYTE: begin
            if (!spi_busy && !spi_done) begin
                spi_tx_byte <= cmd_buf[cmd_idx];
                spi_bit_cnt <= 0; spi_clk_cnt <= 0; spi_busy <= 1;
            end else if (spi_done) begin
                if (cmd_idx < cmd_len - 1) cmd_idx <= cmd_idx + 1;
                else state <= S_SEND_CS_HIGH;
            end
        end
        S_SEND_CS_HIGH: begin
            spi_cs_n <= 1;
            // 触发发送 RTS
            cmd_buf[0] <= MCP_RTS_TX0; cmd_len <= 1; cmd_idx <= 0;
            state <= S_SEND_CS_LOW;  // 复用发送状态，发完后回 IDLE
        end
        S_DONE: state <= S_IDLE;
    endcase
end

endmodule
