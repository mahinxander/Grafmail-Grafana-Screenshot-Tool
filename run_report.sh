#!/bin/bash
# =============================================================================
# GrafMail - Grafana Screenshot Tool - Linux/Unix Wrapper Script
# =============================================================================
# This script orchestrates the execution of the GrafMail-Grafana screenshot tool
# in a Docker container. It handles:
# - Docker availability checks
# - Image loading from tar file (for air-gapped environments)
#
# Usage: ./run_report.sh [--env-file /path/to/.env] [--debug]
# =============================================================================

set -euo pipefail

# =============================================================================
# CONFIGURATION
# =============================================================================

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Docker image configuration
IMAGE_NAME="grafmail"
IMAGE_TAG="latest"
IMAGE_FULL_NAME="${IMAGE_NAME}:${IMAGE_TAG}"

# Tar file for air-gapped transfer
IMAGE_TAR_FILE="${IMAGE_NAME}-${IMAGE_TAG}.tar"

# Default paths
DEFAULT_ENV_FILE="${SCRIPT_DIR}/.env"
DEFAULT_CAPTURES_DIR="${SCRIPT_DIR}/captures"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo ""
    echo "============================================================================="
    echo " $1"
    echo "============================================================================="
    echo ""
}

show_usage() {
    cat << EOF
Grafana Screenshot Tool - Docker Runner

Usage: $0 [OPTIONS]

Options:
    -e, --env-file PATH    Path to environment file (default: .env in script directory)
    -c, --captures PATH    Path to captures directory (default: ./captures)
    -d, --debug            Enable debug mode with verbose output
    -l, --load-tar PATH    Load Docker image from tar file before running
    -h, --help             Show this help message

Examples:
    $0                                     # Run with default .env file
    $0 --env-file /path/to/.env            # Use custom .env file
    $0 -e /path/.env_dashboard1            # Shorthand for custom .env
    $0 --debug                             # Enable debug mode
    $0 --load-tar ./image.tar              # Load image from tar before running

Multi-Dashboard Cronjob Examples:
    # Run different dashboards at different intervals:
    # */30 * * * * /opt/grafana-tool/run_report.sh --env-file /opt/grafana-tool/.env_dashboard1
    # 0 */2 * * *  /opt/grafana-tool/run_report.sh --env-file /opt/grafana-tool/.env_dashboard2
    # 0 8 * * 1    /opt/grafana-tool/run_report.sh --env-file /opt/grafana-tool/.env_weekly

Air-gapped Environment:
    1. Build and save image on internet-connected PC:
       docker build -t ${IMAGE_FULL_NAME} .
       docker save ${IMAGE_FULL_NAME} > ${IMAGE_TAR_FILE}
    
    2. Transfer ${IMAGE_TAR_FILE} to air-gapped PC
    
    3. Run this script on air-gapped PC:
       $0 --load-tar ${IMAGE_TAR_FILE}
EOF
}

# =============================================================================
# VALIDATION FUNCTIONS
# =============================================================================

check_docker_installed() {
    print_header "Checking Docker Installation"
    
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed on this system"
        echo ""
        echo "Please install Docker using one of the following methods:"
        echo ""
        echo "  Ubuntu/Debian:"
        echo "    curl -fsSL https://get.docker.com | sh"
        echo "    sudo usermod -aG docker \$USER"
        echo ""
        echo "  CentOS/RHEL:"
        echo "    sudo yum install -y docker"
        echo "    sudo systemctl enable --now docker"
        echo ""
        echo "  Fedora:"
        echo "    sudo dnf install -y docker"
        echo "    sudo systemctl enable --now docker"
        echo ""
        exit 1
    fi
    
    # Check if Docker daemon is running
    if ! docker info &> /dev/null; then
        log_error "Docker daemon is not running"
        echo ""
        echo "Start Docker daemon with:"
        echo "  sudo systemctl start docker"
        echo ""
        echo "Or enable it to start on boot:"
        echo "  sudo systemctl enable --now docker"
        echo ""
        exit 1
    fi
    
    local docker_version
    docker_version=$(docker --version)
    log_success "Docker is installed and running: $docker_version"
}

check_docker_image() {
    print_header "Checking Docker Image"
    
    if docker image inspect "${IMAGE_FULL_NAME}" &> /dev/null; then
        local image_size
        image_size=$(docker image inspect "${IMAGE_FULL_NAME}" --format='{{.Size}}' 2>/dev/null | numfmt --to=iec-i --suffix=B 2>/dev/null || docker image inspect "${IMAGE_FULL_NAME}" --format='{{.Size}}' 2>/dev/null || echo "unknown")
        log_success "Docker image found: ${IMAGE_FULL_NAME} (${image_size})"
        return 0
    else
        log_warning "Docker image not found: ${IMAGE_FULL_NAME}"
        return 1
    fi
}

load_docker_image() {
    local tar_path="$1"
    
    print_header "Loading Docker Image from Tar File"
    
    if [[ ! -f "$tar_path" ]]; then
        log_error "Tar file not found: $tar_path"
        exit 1
    fi
    
    log_info "Loading image from: $tar_path"
    
    local file_size
    file_size=$(stat -c%s "$tar_path" 2>/dev/null | numfmt --to=iec-i --suffix=B 2>/dev/null || stat -f%z "$tar_path" 2>/dev/null || echo "unknown")
    log_info "File size: ${file_size}"
    
    if docker load -i "$tar_path"; then
        log_success "Docker image loaded successfully"
    else
        log_error "Failed to load Docker image"
        exit 1
    fi
}

check_env_file() {
    local env_file="$1"
    
    print_header "Checking Environment Configuration"
    
    if [[ ! -f "$env_file" ]]; then
        log_error "Environment file not found: $env_file"
        echo ""
        echo "Please create a .env file with your configuration:"
        echo "  cp .env.example .env"
        echo "  # Edit .env with your settings"
        echo ""
        exit 1
    fi
    
    # Check if file has actual content 
    local has_content
    has_content=$(grep -v '^\s*#' "$env_file" | grep -v '^\s*$' | wc -l)
    
    if [[ $has_content -eq 0 ]]; then
        log_warning "Environment file appears to be empty or contain only comments"
    fi
    
    # Mask sensitive values in output
    log_success "Environment file found: $env_file"
    log_info "Configuration loaded (sensitive values masked)"
}

setup_captures_dir() {
    local captures_dir="$1"
    
    if [[ ! -d "$captures_dir" ]]; then
        log_info "Creating captures directory: $captures_dir"
        mkdir -p "$captures_dir"
    fi
    
    log_success "Captures directory ready: $captures_dir"
}

# =============================================================================
# MAIN EXECUTION
# =============================================================================

run_container() {
    local env_file="$1"
    local captures_dir="$2"
    local debug="$3"
    
    print_header "Running Grafana Screenshot Tool"
    
    # Convert paths to absolute
    local abs_env_file
    local abs_captures_dir
    abs_env_file=$(realpath "$env_file")
    abs_captures_dir=$(realpath "$captures_dir")
    
    log_info "Environment file: $abs_env_file"
    log_info "Captures directory: $abs_captures_dir"
    
    # Build docker run command
    local docker_args=(
        "run"
        "--rm"
        "--name" "grafmail-$$"
        "--user" "$(id -u):$(id -g)"
        "--volume" "${abs_env_file}:/app/.env:ro,Z"
        "--volume" "${abs_captures_dir}:/app/captures:Z"
    )
    log_info "Running container as UID=$(id -u), GID=$(id -g)"
    
    # ── Paramiko SSH key mounts ────────────────────────────────────────────
    local remote_method
    remote_method=$(grep -E '^REMOTE_COPY_METHOD=' "$env_file" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | xargs)
    if [[ "${remote_method:-paramiko}" == "paramiko" ]]; then
        
        docker_args+=("--env" "HOME=/tmp")
        log_info "Set container HOME=/tmp for SSH directory access"
        
        # Check if SSH_KEY_PATH is explicitly set in .env (user specified a custom path)
        local env_ssh_key
        env_ssh_key=$(grep -E '^SSH_KEY_PATH=' "$env_file" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | xargs)
        
        if [[ -z "$env_ssh_key" ]]; then
            
            local host_ssh_dir="${HOME}/.ssh"
            
            if [[ -f "${host_ssh_dir}/id_rsa" ]]; then
                docker_args+=("--volume" "${host_ssh_dir}/id_rsa:/tmp/ssh_key:ro,Z")
                docker_args+=("--env" "SSH_KEY_PATH=/tmp/ssh_key")
                log_info "Mounting SSH key: ${host_ssh_dir}/id_rsa (RSA)"
            elif [[ -f "${host_ssh_dir}/id_ed25519" ]]; then
                docker_args+=("--volume" "${host_ssh_dir}/id_ed25519:/tmp/ssh_key:ro,Z")
                docker_args+=("--env" "SSH_KEY_PATH=/tmp/ssh_key")
                log_info "Mounting SSH key: ${host_ssh_dir}/id_ed25519 (Ed25519)"
            elif [[ -f "${host_ssh_dir}/id_ecdsa" ]]; then
                docker_args+=("--volume" "${host_ssh_dir}/id_ecdsa:/tmp/ssh_key:ro,Z")
                docker_args+=("--env" "SSH_KEY_PATH=/tmp/ssh_key")
                log_info "Mounting SSH key: ${host_ssh_dir}/id_ecdsa (ECDSA)"
            else
                log_warning "No SSH key found in ${host_ssh_dir}/ — paramiko may fail to authenticate."
                log_warning "Set SSH_PASSWORD in .env for password auth, or generate a key with: ssh-keygen -t ed25519"
            fi
            
            # Mount known_hosts if it exists on the host (needed for SSH_HOST_KEY_POLICY=reject)
            if [[ -f "${host_ssh_dir}/known_hosts" ]]; then
                docker_args+=("--volume" "${host_ssh_dir}/known_hosts:/tmp/.ssh/known_hosts:ro,Z")
                log_info "Mounting known_hosts: ${host_ssh_dir}/known_hosts"
            fi
        fi

        # Check for password auth as an alternative if no key was found
        local env_ssh_password
        env_ssh_password=$(grep -E '^SSH_PASSWORD=' "$env_file" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | xargs)
        if [[ -n "$env_ssh_password" ]]; then
            log_info "SSH_PASSWORD detected — will use password authentication (no key needed)"
        fi
    fi
    
    # ── Docker resource limits (configurable via .env) ─────────────────────
    local docker_memory docker_cpus
    docker_memory=$(grep -E '^DOCKER_MEMORY=' "$env_file" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | xargs)
    docker_cpus=$(grep -E '^DOCKER_CPUS=' "$env_file" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" | xargs)
    docker_memory="${docker_memory:-1g}"
    docker_cpus="${docker_cpus:-1.5}"
    docker_args+=("--memory" "${docker_memory}")
    docker_args+=("--cpus" "${docker_cpus}")
    log_info "Docker limits: memory=${docker_memory}, cpus=${docker_cpus}"
    
    
    docker_args+=(
        "--network=host"
        "--security-opt" "no-new-privileges"
        "--env" "ENV_FILE=/app/.env"
    )
    
    # Add debug flag if enabled
    if [[ "$debug" == "true" ]]; then
        docker_args+=("--env" "DEBUG_MODE=true")
        log_info "Debug mode enabled"
    fi
    
    # Add image name
    docker_args+=("${IMAGE_FULL_NAME}")
    
    log_info "Starting container..."
    echo ""
    
    # Run container
    if docker "${docker_args[@]}"; then
        echo ""
        print_header "Execution Complete"
        log_success "Screenshot captured and saved to: $abs_captures_dir"
        
        # List captured files (recursive for UID subdirectories)
        local file_count
        file_count=$(find "${abs_captures_dir}" -type f \( -name '*.png' -o -name '*.pdf' \) 2>/dev/null | wc -l)
        if [[ "$file_count" -gt 0 ]]; then
            echo ""
            log_info "Captured files:"
            find "${abs_captures_dir}" -type f \( -name '*.png' -o -name '*.pdf' \) -exec ls -lh {} \; 2>/dev/null || true
        fi
        
        return 0
    else
        local exit_code=$?
        echo ""
        log_error "Container execution failed with exit code: $exit_code"
        return $exit_code
    fi
}

main() {
    local env_file="${DEFAULT_ENV_FILE}"
    local captures_dir="${DEFAULT_CAPTURES_DIR}"
    local debug="false"
    local load_tar=""
    
    # Parse command line arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            -e|--env-file)
                env_file="$2"
                shift 2
                ;;
            -c|--captures)
                captures_dir="$2"
                shift 2
                ;;
            -d|--debug)
                debug="true"
                shift
                ;;
            -l|--load-tar)
                load_tar="$2"
                shift 2
                ;;
            -h|--help)
                show_usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                echo ""
                show_usage
                exit 1
                ;;
        esac
    done
    
    # Print banner
    echo ""
    echo "   ██████╗ ██████╗  █████╗ ███████╗███╗   ███╗ █████╗ ██╗██╗     "
    echo "  ██╔════╝ ██╔══██╗██╔══██╗██╔════╝████╗ ████║██╔══██╗██║██║     "
    echo "  ██║  ███╗██████╔╝███████║█████╗  ██╔████╔██║███████║██║██║     "
    echo "  ██║   ██║██╔══██╗██╔══██║██╔══╝  ██║╚██╔╝██║██╔══██║██║██║     "
    echo "  ╚██████╔╝██║  ██║██║  ██║██║     ██║ ╚═╝ ██║██║  ██║██║███████╗"
    echo "   ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝╚══════╝"
    echo ""
    echo "                GrafMail v1.0.0"
    echo "      Offline-Ready Grafana Screenshot & Email Tool"
    echo ""
    
    # Run checks
    check_docker_installed
    
    # Load image from tar if specified
    if [[ -n "$load_tar" ]]; then
        load_docker_image "$load_tar"
    fi
    
    # Check if image exists
    if ! check_docker_image; then
        log_error "Docker image not available. Options:"
        echo ""
        echo "  1. Build the image:"
        echo "     docker build -t ${IMAGE_FULL_NAME} ."
        echo ""
        echo "  2. Load from tar file (air-gapped):"
        echo "     $0 --load-tar ${IMAGE_TAR_FILE}"
        echo ""
        echo "  3. Load from tar file (any location):"
        echo "     $0 --load-tar /path/to/image.tar"
        echo ""
        exit 1
    fi
    
    check_env_file "$env_file"
    setup_captures_dir "$captures_dir"
    
    # Run the container
    run_container "$env_file" "$captures_dir" "$debug"
}

# Run main function
main "$@"

