#!/usr/bin/env bash
# admin/lib/common.sh — Colors and print helpers

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
fail() { echo -e "${RED}  ✗${NC}  $*"; }
warn() { echo -e "${YELLOW}  !${NC}  $*"; }
info() { echo -e "${CYAN}  ▸${NC}  $*"; }
header() { echo -e "\n${BOLD}$*${NC}"; }
