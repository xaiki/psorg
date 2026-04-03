#!/bin/bash
# create_pfs_image.sh - Create a PFS filesystem image for PS5 FFPFS loaders
# 
# Usage: ./create_pfs_image.sh <input_dir> [output_file]

set -e

# Colors and emojis
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m'

# Emojis
INFO_EMOJI="ℹ️"
SUCCESS_EMOJI="✅"
WARNING_EMOJI="⚠️"
ERROR_EMOJI="❌"
PROGRESS_EMOJI="🔄"
CHECK_EMOJI="🔍"
HAMMER_EMOJI="🔨"
DISK_EMOJI="💾"
MOUNT_EMOJI="🔗"
GEAR_EMOJI="⚙️"
PACKAGE_EMOJI="📦"
GAME_EMOJI="🎮"
API_EMOJI="🌐"
TAG_EMOJI="🏷️"
MAGIC_EMOJI="✨"

# Print functions
print_info() { echo -e "${BLUE}${INFO_EMOJI} INFO:${NC} $1"; }
print_success() { echo -e "${GREEN}${SUCCESS_EMOJI} SUCCESS:${NC} $1"; }
print_warning() { echo -e "${YELLOW}${WARNING_EMOJI} WARNING:${NC} $1"; }
print_error() { echo -e "${RED}${ERROR_EMOJI} ERROR:${NC} $1" >&2; }
print_progress() { echo -e "${CYAN}${PROGRESS_EMOJI} $1${NC}"; }
print_check() { echo -e "${PURPLE}${CHECK_EMOJI} CHECKING:${NC} $1"; }

# Configuration
RAWG_API_KEY=""
CONFIG_FILE="$HOME/.config/ps5-pfs-creator.cfg"
OUTPUT_FILE=""  # Will be set later

# Load config
load_config() {
    if [ -f "$CONFIG_FILE" ]; then
        source "$CONFIG_FILE"
        print_info "Loaded configuration from $CONFIG_FILE"
    fi
}

# Save config
save_config() {
    mkdir -p "$(dirname "$CONFIG_FILE")"
    echo "RAWG_API_KEY=\"$RAWG_API_KEY\"" > "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    print_success "API key saved to $CONFIG_FILE"
}

# Get API key
get_api_key() {
    if [ -n "$RAWG_API_KEY" ]; then
        return 0
    fi
    
    if [ ! -t 0 ]; then
        print_warning "Non-interactive mode - skipping game name lookup"
        return 1
    fi
    
    print_warning "RAWG.io API key not found"
    echo ""
    echo "Get a free API key from: https://rawg.io/apidocs"
    echo ""
    read -p "Enter your RAWG API key (or press Enter to skip): " user_key
    
    if [ -n "$user_key" ]; then
        RAWG_API_KEY="$user_key"
        save_config
        return 0
    fi
    return 1
}

# Extract PPSA code
get_ppsa_code() {
    local dir="$1"
    local dir_name=$(basename "$dir")
    
    # Try directory name first
    local code=$(echo "$dir_name" | grep -oE 'PPSA[0-9]{5,}' | head -1)
    if [ -n "$code" ]; then
        echo "$code"
        return 0
    fi
    
    # Try param.sfo
    if [ -f "$dir/sce_sys/param.sfo" ]; then
        code=$(strings "$dir/sce_sys/param.sfo" 2>/dev/null | grep -oE 'PPSA[0-9]{5,}' | head -1)
        if [ -n "$code" ]; then
            echo "$code"
            return 0
        fi
    fi
    
    return 1
}

# Get game title
get_game_title() {
    local dir="$1"
    local dir_name=$(basename "$dir")
    
    # Try to extract from directory name (remove PPSA code)
    local title=$(echo "$dir_name" | sed -E 's/[.-]PPSA[0-9]+.*$//' | tr '.' ' ')
    if [ -n "$title" ] && [ ${#title} -gt 2 ]; then
        echo "$title"
        return 0
    fi
    
    # Try param.sfo
    if [ -f "$dir/sce_sys/param.sfo" ]; then
        title=$(strings "$dir/sce_sys/param.sfo" 2>/dev/null | grep -A1 "^TITLE$" | tail -1 2>/dev/null)
        if [ -n "$title" ] && [ ${#title} -gt 2 ]; then
            echo "$title"
            return 0
        fi
    fi
    
    return 1
}

# Lookup game on RAWG.io

# Lookup game on RAWG.io
lookup_game() {
    local search="$1"
    if [ -z "$RAWG_API_KEY" ] || [ -z "$search" ]; then return 1; fi
    
    # Redirect UI feedback to stderr so it doesn't break variable assignment
    print_progress "Looking up: $search" >&2 
    
    local encoded=$(echo "$search" | sed -e 's/ /%20/g')
    local url="https://api.rawg.io/api/games?key=$RAWG_API_KEY&search=$encoded&page_size=1&platforms=187,18,16"
    local response=$(curl -s --max-time 10 "$url" 2>/dev/null)
    
    if [ -n "$response" ]; then
        local name=$(echo "$response" | jq -r '.results[0].name // empty' 2>/dev/null)
        if [ -n "$name" ]; then
            echo "$name"
            return 0
        fi
    fi
    return 1
}

# Sanitize filename
sanitize_name() {
    echo "$1" | tr ' ' '.' | sed 's/[^a-zA-Z0-9.]/-/g' | sed 's/\.\.\+/./g' | sed 's/^\.//;s/\.$//'
}

# Generate filename (pure function, no output)
generate_filename() {
    local dir="$1"
    local ppsa="$2"
    local name=""
    
    # Try API lookup first
    if [ -n "$ppsa" ] && [ -n "$RAWG_API_KEY" ]; then
        name=$(lookup_game "$ppsa")
    fi
    
    # Fallback to directory name
    if [ -z "$name" ]; then
        name=$(get_game_title "$dir")
    fi
    
    # Final fallback
    if [ -z "$name" ]; then
        name="Game"
    fi
    
    local safe_name=$(sanitize_name "$name")
    
    if [ -n "$ppsa" ]; then
        echo "${safe_name}-${ppsa}.ffpfs"
    else
        echo "${safe_name}.ffpfs"
    fi
}

# Check dependencies
check_deps() {
    print_check "Checking dependencies..."
    
    local missing=()
    
    for cmd in gcc make git rsync fusermount pkg-config curl strings; do
        if ! command -v "$cmd" &>/dev/null; then
            case "$cmd" in
                fusermount) missing+=("fuse3") ;;
                strings) missing+=("binutils") ;;
                *) missing+=("$cmd") ;;
            esac
        fi
    done
    
    if ! pkg-config --exists fuse3 2>/dev/null; then
        missing+=("libfuse3-dev")
    fi
    
    if [ ${#missing[@]} -gt 0 ]; then
        print_warning "Missing: ${missing[*]}"
        
        if command -v apt-get &>/dev/null; then
            sudo apt-get update
            sudo apt-get install -y build-essential git rsync fuse3 libfuse3-dev pkg-config curl binutils jq
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y gcc make git rsync fuse3 fuse3-devel pkgconfig curl binutils jq
        elif command -v pacman &>/dev/null; then
            sudo pacman -S --noconfirm base-devel git rsync fuse3 curl binutils jq
        else
            print_error "Please install dependencies manually"
            exit 1
        fi
    fi
    
    if command -v jq &>/dev/null; then
        print_success "jq available"
    else
        print_warning "jq not found (optional)"
    fi
}

# Setup pfsshell
setup_pfsshell() {
    print_check "Setting up pfsshell..."
    
    if command -v pfsfuse &>/dev/null && command -v pfsshell &>/dev/null; then
        print_success "pfsshell utilities already installed"
        return 0
    fi

    local tmp="/tmp/pfsshell"
    [ ! -d "$tmp" ] && git clone --recursive https://github.com/ps2homebrew/pfsshell.git "$tmp"
    
    cd "$tmp"
    git submodule update --init --recursive

    # Build the core logic
    rm -rf build
    meson setup build && ninja -C build

    # Install the main shell
    if [ -f "build/pfsshell" ]; then
        sudo cp build/pfsshell /usr/local/bin/
        print_success "Installed pfsshell"
    fi

    # MANUAL COMPILATION OF PFSFUSE (If Meson skipped it)
    if [ ! -f "build/pfsfuse" ]; then
        print_warning "Meson skipped pfsfuse. Attempting manual compile..."
        # We link the pfsfuse.c against the static libraries Meson just built
        gcc -O2 -o pfsfuse src/pfsfuse.c \
            -Iinclude -Isubprojects/apa/include -Isubprojects/pfs/include -Isubprojects/iomanX/include \
            -Lbuild/subprojects/apa -Lbuild/subprojects/pfs -Lbuild/subprojects/iomanX \
            -lpfs -lapa -liomanX \
            $(pkg-config --cflags --libs fuse3 2>/dev/null || pkg-config --cflags --libs fuse) \
            -D_FILE_OFFSET_BITS=64
        
        if [ -f "pfsfuse" ]; then
            sudo cp pfsfuse /usr/local/bin/
            print_success "Manually compiled and installed pfsfuse"
        else
            print_error "Manual compilation failed. Check if libfuse3-dev or libfuse-dev is installed."
            exit 1
        fi
    else
        sudo cp build/pfsfuse /usr/local/bin/
        print_success "Installed pfsfuse"
    fi
}

# Validate directory
validate_dir() {
    local dir="$1"
    local issues=0
    
    print_check "Validating directory..."
    
    if [ ! -f "$dir/eboot.bin" ]; then
        print_warning "eboot.bin not found"
        issues=$((issues + 1))
    else
        print_success "Found eboot.bin"
    fi
    
    if [ ! -d "$dir/sce_sys" ]; then
        print_warning "sce_sys not found"
        issues=$((issues + 1))
    else
        print_success "Found sce_sys"
        if [ ! -f "$dir/sce_sys/param.sfo" ]; then
            print_warning "param.sfo not found"
            issues=$((issues + 1))
        else
            print_success "Found param.sfo"
        fi
    fi
    
    if [ $issues -eq 0 ]; then
        print_success "Directory looks good!"
    else
        print_warning "Found $issues issue(s)"
    fi
}

# Calculate size
calc_size() {
    local dir="$1"
    
    local bytes=$(find "$dir" -type f -printf '%s\n' 2>/dev/null | awk '{sum+=$1} END {print sum+0}')
    local files=$(find "$dir" -type f 2>/dev/null | wc -l)
    local dirs=$(find "$dir" -type d 2>/dev/null | wc -l)
    
    # PFS overhead: 4KB superblock + 128B per file + 64B per dir + 64MB safety
    local overhead=$(( (files * 128) + (dirs * 64) + (4*1024) + (64*1024*1024) ))
    local total=$(( (bytes + overhead + 1024*1024 - 1) / (1024*1024) ))
    
    echo "$total $bytes $files $dirs"
}

# Main
echo -e "\n${GREEN}${PACKAGE_EMOJI}${MAGIC_EMOJI} PS5 PFS Creator${NC}"
echo "=================================================="

# Load config
load_config

# Check args
if [ -z "$1" ]; then
    echo "Usage: $0 <input_dir> [output_file]"
    exit 1
fi

INPUT_DIR=$(realpath "$1")
if [ ! -d "$INPUT_DIR" ]; then
    print_error "Directory not found: $INPUT_DIR"
    exit 1
fi

print_info "Input: $INPUT_DIR"

# Validate
validate_dir "$INPUT_DIR"

# Get PPSA code
PPSA_CODE=$(get_ppsa_code "$INPUT_DIR" || echo "")
if [ -n "$PPSA_CODE" ]; then
    print_success "PPSA code: $PPSA_CODE"
fi

# Get API key if needed
if [ -z "$2" ]; then
    get_api_key || true
fi

# Generate filename if not provided
if [ -z "$2" ]; then
    print_progress "Generating filename..."
    OUTPUT_FILE=$(generate_filename "$INPUT_DIR" "$PPSA_CODE")
    print_success "Filename: $OUTPUT_FILE"
else
    OUTPUT_FILE="$2"
fi

# Make output absolute
if [[ "$OUTPUT_FILE" != /* ]]; then
    OUTPUT_FILE="$(pwd)/$OUTPUT_FILE"
fi
print_info "Output: $OUTPUT_FILE"

# Check deps
check_deps
setup_pfsshell

# Calculate size
read total_mb raw_bytes files dirs <<< $(calc_size "$INPUT_DIR")
print_info "Raw data: $(numfmt --to=iec-i --suffix=B $raw_bytes 2>/dev/null || echo "$raw_bytes bytes")"
print_info "Files: $files, Dirs: $dirs"
print_info "Image size: ${total_mb}MB"

# Check if output exists
if [ -f "$OUTPUT_FILE" ]; then
    print_warning "Output exists"
    read -p "Overwrite? (y/N): " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        print_error "Aborted"
        exit 1
    fi
fi

# Create image
print_progress "Creating image..."
truncate -s "${total_mb}M" "$OUTPUT_FILE"
print_success "Image created"

# Mount
MOUNT="/mnt/pfs_$$"
sudo mkdir -p "$MOUNT"

print_progress "Mounting..."
if ! sudo pfsfuse -o allow_other,nonempty "$OUTPUT_FILE" "$MOUNT" 2>/dev/null; then
    print_error "Mount failed"
    sudo rmdir "$MOUNT" 2>/dev/null
    exit 1
fi
print_success "Mounted at $MOUNT"

# Copy
print_progress "Copying files..."
rsync -a --info=progress2 "$INPUT_DIR/" "$MOUNT/"
print_success "Copy complete"

# Unmount
print_progress "Unmounting..."
sudo fusermount -u "$MOUNT" 2>/dev/null || sudo umount "$MOUNT" 2>/dev/null
sudo rmdir "$MOUNT" 2>/dev/null

# Done
echo ""
print_success "${PACKAGE_EMOJI}${MAGIC_EMOJI} Success!"
echo "=================================================="
echo "📦 Output: $OUTPUT_FILE"
echo "📏 Size: $(numfmt --to=iec-i --suffix=B $(stat -c%s "$OUTPUT_FILE") 2>/dev/null || echo "$(du -h "$OUTPUT_FILE" | cut -f1)")"
[ -n "$PPSA_CODE" ] && echo "🎮 PPSA: $PPSA_CODE"

TITLE=$(get_game_title "$INPUT_DIR")
[ -n "$TITLE" ] && echo "📝 Title: $TITLE"

echo ""
print_info "Next: Copy to your FFPFS loader folder"
