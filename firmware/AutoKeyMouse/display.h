#pragma once
// TFT への状態表示 (Wi-Fi の IP / BLE / マクロ)。HAS_TFT=0 なら何もしない。
namespace display {

void begin();
void loop();  // loop() から呼ぶ。内容が変わったときだけ描き直す

}  // namespace display
