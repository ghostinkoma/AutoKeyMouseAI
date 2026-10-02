#include "keymap.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

namespace keymap {
namespace {

struct NamedKey {
  const char* name;
  uint8_t code;
};

// clang-format off
const NamedKey kNamed[] = {
  {"enter", 0x28}, {"return", 0x28}, {"esc", 0x29}, {"escape", 0x29},
  {"backspace", 0x2A}, {"bs", 0x2A}, {"tab", 0x2B}, {"space", 0x2C},
  {"minus", 0x2D}, {"equal", 0x2E}, {"lbracket", 0x2F}, {"rbracket", 0x30},
  {"backslash", 0x31}, {"semicolon", 0x33}, {"quote", 0x34}, {"grave", 0x35},
  {"comma", 0x36}, {"dot", 0x37}, {"period", 0x37}, {"slash", 0x38},
  {"capslock", 0x39},
  {"printscreen", 0x46}, {"scrolllock", 0x47}, {"pause", 0x48},
  {"insert", 0x49}, {"ins", 0x49}, {"home", 0x4A}, {"pageup", 0x4B}, {"pgup", 0x4B},
  {"delete", 0x4C}, {"del", 0x4C}, {"end", 0x4D}, {"pagedown", 0x4E}, {"pgdn", 0x4E},
  {"right", 0x4F}, {"left", 0x50}, {"down", 0x51}, {"up", 0x52},
  {"numlock", 0x53}, {"kp_div", 0x54}, {"kp_mul", 0x55}, {"kp_minus", 0x56},
  {"kp_plus", 0x57}, {"kp_enter", 0x58},
  {"kp1", 0x59}, {"kp2", 0x5A}, {"kp3", 0x5B}, {"kp4", 0x5C}, {"kp5", 0x5D},
  {"kp6", 0x5E}, {"kp7", 0x5F}, {"kp8", 0x60}, {"kp9", 0x61}, {"kp0", 0x62},
  {"kp_dot", 0x63}, {"menu", 0x65}, {"app", 0x65},
  // JIS (109) 固有キー
  {"ro", 0x87}, {"kana", 0x88}, {"yen", 0x89}, {"henkan", 0x8A}, {"muhenkan", 0x8B},
  {"zenkaku", 0x35}, {"hankaku", 0x35},
  // 修飾キー
  {"ctrl", 0xE0}, {"lctrl", 0xE0}, {"control", 0xE0},
  {"shift", 0xE1}, {"lshift", 0xE1},
  {"alt", 0xE2}, {"lalt", 0xE2},
  {"win", 0xE3}, {"gui", 0xE3}, {"lwin", 0xE3}, {"cmd", 0xE3},
  {"rctrl", 0xE4}, {"rshift", 0xE5}, {"ralt", 0xE6}, {"rwin", 0xE7}, {"rgui", 0xE7},
};

// US 配列の記号 (ASCII 0x20-0x7E)。上位ビット 0x80 = Shift。
const uint8_t SHIFT = 0x80;
const uint8_t kAsciiUS[95] = {
  0x2C,         0x1E|SHIFT, 0x34|SHIFT, 0x20|SHIFT, 0x21|SHIFT, 0x22|SHIFT, 0x24|SHIFT, 0x34,        //  !"#$%&'
  0x26|SHIFT,   0x27|SHIFT, 0x25|SHIFT, 0x2E|SHIFT, 0x36,       0x2D,       0x37,       0x38,        // ()*+,-./
  0x27, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24,                                                   // 01234567
  0x25, 0x26, 0x33|SHIFT, 0x33, 0x36|SHIFT, 0x2E, 0x37|SHIFT, 0x38|SHIFT,                           // 89:;<=>?
  0x1F|SHIFT,                                                                                       // @
  0x04|SHIFT, 0x05|SHIFT, 0x06|SHIFT, 0x07|SHIFT, 0x08|SHIFT, 0x09|SHIFT, 0x0A|SHIFT, 0x0B|SHIFT,   // A-H
  0x0C|SHIFT, 0x0D|SHIFT, 0x0E|SHIFT, 0x0F|SHIFT, 0x10|SHIFT, 0x11|SHIFT, 0x12|SHIFT, 0x13|SHIFT,   // I-P
  0x14|SHIFT, 0x15|SHIFT, 0x16|SHIFT, 0x17|SHIFT, 0x18|SHIFT, 0x19|SHIFT, 0x1A|SHIFT, 0x1B|SHIFT,   // Q-X
  0x1C|SHIFT, 0x1D|SHIFT,                                                                           // Y-Z
  0x2F, 0x31, 0x30, 0x23|SHIFT, 0x2D|SHIFT, 0x35,                                                   // [\]^_`
  0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10,                     // a-m
  0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D,                     // n-z
  0x2F|SHIFT, 0x31|SHIFT, 0x30|SHIFT, 0x35|SHIFT,                                                   // {|}~
};

// JIS 配列で US と異なる記号
struct JpOverride {
  char c;
  uint8_t code;
};
const JpOverride kJp[] = {
  {'"', 0x1F|SHIFT}, {'&', 0x23|SHIFT}, {'\'', 0x24|SHIFT}, {'(', 0x25|SHIFT},
  {')', 0x26|SHIFT}, {'=', 0x2D|SHIFT}, {'^', 0x2E}, {'~', 0x2E|SHIFT},
  {'@', 0x2F}, {'`', 0x2F|SHIFT}, {'[', 0x30}, {'{', 0x30|SHIFT},
  {']', 0x32}, {'}', 0x32|SHIFT}, {':', 0x34}, {'*', 0x34|SHIFT},
  {'+', 0x33|SHIFT}, {'\\', 0x87}, {'_', 0x87|SHIFT}, {'|', 0x89|SHIFT},
};
// clang-format on

}  // namespace

uint8_t keyFromName(const char* name) {
  if (!name || !*name) return 0;
  size_t len = strlen(name);

  if (len == 1) {
    char c = tolower((unsigned char)name[0]);
    if (c >= 'a' && c <= 'z') return 0x04 + (c - 'a');
    if (c >= '1' && c <= '9') return 0x1E + (c - '1');
    if (c == '0') return 0x27;
  }
  if ((name[0] == 'f' || name[0] == 'F') && len <= 3 && isdigit((unsigned char)name[1])) {
    int n = atoi(name + 1);
    if (n >= 1 && n <= 12) return 0x3A + (n - 1);
    if (n >= 13 && n <= 24) return 0x68 + (n - 13);
  }
  if (len > 2 && name[0] == '0' && (name[1] == 'x' || name[1] == 'X')) {
    return (uint8_t)strtoul(name + 2, nullptr, 16);
  }
  for (const auto& k : kNamed)
    if (strcasecmp(k.name, name) == 0) return k.code;
  return 0;
}

bool keyFromChar(char c, Layout layout, uint8_t* code, bool* shift) {
  uint8_t v = 0;
  if (c == '\n') {
    v = 0x28;
  } else if (c == '\t') {
    v = 0x2B;
  } else if (c >= 0x20 && c <= 0x7E) {
    v = kAsciiUS[c - 0x20];
    if (layout == Layout::JP) {
      for (const auto& o : kJp)
        if (o.c == c) v = o.code;
    }
  } else {
    return false;
  }
  *code = v & 0x7F;
  // 0x87 (Ro) / 0x89 (Yen) は 0x80 を超えるので Shift ビットと区別する
  if (layout == Layout::JP && (c == '\\' || c == '_' || c == '|')) {
    *code = (c == '|') ? 0x89 : 0x87;
    *shift = (c != '\\');
    return true;
  }
  *shift = (v & SHIFT) != 0;
  return true;
}

}  // namespace keymap
