// WS-432: CLI wrapper around the PRODUCTION NVS boot policy.
//
// No policy logic here - it parses one esp_err_t per line and prints what
// src/nvs_boot_policy.h decides. host/test_nvs_boot_policy.py compiles this
// with g++, so the assertions run against the same header the firmware uses.
//
// Commands (one per line on stdin):
//
//   init <err>   -> verdict=usable|erase|unusable may_erase=0|1
//   erase <err>  -> 0|1

#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include "nvs_boot_policy.h"

namespace {

int parseErr(const std::string& arg, long* out) {
  char* end = nullptr;
  const long v = std::strtol(arg.c_str(), &end, 0);
  if (end == arg.c_str() || *end != '\0') return -1;
  *out = v;
  return 0;
}

const char* verdictName(nvsboot::Verdict v) {
  switch (v) {
    case nvsboot::VERDICT_USABLE: return "usable";
    case nvsboot::VERDICT_ERASE: return "erase";
    default: return "unusable";
  }
}

int doInit(const std::string& arg) {
  long v = 0;
  if (parseErr(arg, &v) != 0) { std::printf("err=usage\n"); return 1; }
  std::printf("verdict=%s may_erase=%d\n", verdictName(nvsboot::classifyInit((int)v)),
              nvsboot::mayErase((int)v) ? 1 : 0);
  return 0;
}

int doErase(const std::string& arg) {
  long v = 0;
  if (parseErr(arg, &v) != 0) { std::printf("err=usage\n"); return 1; }
  std::printf("%d\n", nvsboot::mayErase((int)v) ? 1 : 0);
  return 0;
}

}  // namespace

int main() {
  std::string line;
  while (std::getline(std::cin, line)) {
    std::vector<std::string> tok;
    size_t i = 0;
    while (i < line.size()) {
      while (i < line.size() && std::isspace((unsigned char)line[i])) i++;
      size_t start = i;
      while (i < line.size() && !std::isspace((unsigned char)line[i])) i++;
      if (i > start) tok.push_back(line.substr(start, i - start));
    }
    if (tok.empty()) continue;
    int rc = 0;
    if (tok[0] == "init" && tok.size() >= 2)       rc = doInit(tok[1]);
    else if (tok[0] == "erase" && tok.size() >= 2) rc = doErase(tok[1]);
    else { std::printf("err=usage\n"); rc = 1; }
    std::fflush(stdout);
    if (rc != 0) return rc;
  }
  return 0;
}
