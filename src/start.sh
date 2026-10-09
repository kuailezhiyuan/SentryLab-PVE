#!/bin/bash

#
# @file start.sh
# @author CmPi <cmpi@webe.fr>
# @brief Activate SentryLab systemd services and timers
# @date creation 2025-12-29
# @version 1.0.363
# @usage sudo /usr/local/bin/sentrylab/start.sh
#

set -euo pipefail

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root" 
   exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load shared utility functions and configuration
if [ -f "$SCRIPT_DIR/utils.sh" ]; then
    source "$SCRIPT_DIR/utils.sh"
else
    echo "ERROR: Required file '$SCRIPT_DIR/utils.sh' not found." >&2
    exit 1
fi

# Force DEBUG mode for interactive admin tasks
DEBUG=true

# VERBOSE mode: show detailed systemctl output (symlink creation/removal)
# Set to false to only show summary messages
VERBOSE=${VERBOSE:-true}

SYSTEMD_STAGING="$SCRIPT_DIR/system"
SYSTEMD_LIVE="/etc/systemd/system"

box_title "Activating SentryLab Services"

# Verify staging directory exists
if [ ! -d "$SYSTEMD_STAGING" ]; then
    box_line "ERROR: Systemd staging directory not found at $SYSTEMD_STAGING"
    exit 1
fi

# Check for backup files
BACKUP_DIR="$SCRIPT_DIR/systemd_backup"
has_backup=false
if [ -d "$BACKUP_DIR" ] && ls "$BACKUP_DIR"/*.service >/dev/null 2>&1 || ls "$BACKUP_DIR"/*.timer >/dev/null 2>&1; then
    has_backup=true
fi

# Determine which files to use
SOURCE_DIR="$SYSTEMD_STAGING"
if [ "$has_backup" = true ]; then
    box_begin "Unit Files Available"
    box_line "Default units in: $SYSTEMD_STAGING"
    box_line "Customized backup in: $BACKUP_DIR"
    box_end
    echo
    
    # Ask user which version to install
    box_begin "Which version would you like to install?"
    box_line "  1) Default (from staging directory)"
    box_line "  2) Customized (from backup directory)"
    read -p "Enter choice (1 or 2): " choice
    case "$choice" in
        2)
            SOURCE_DIR="$BACKUP_DIR"
            box_line "INFO: Using customized backup units" "GREEN"
            ;;
        1|*)
            SOURCE_DIR="$SYSTEMD_STAGING"
            box_line "INFO: Using default units" "GREEN"
            ;;
    esac
    box_end
fi

box_begin "Deploying Systemd Units"
box_value "Source directory" "$SOURCE_DIR"
box_value "Target directory" "$SYSTEMD_LIVE"

# Move service and timer files from staging to live
if ls "$SOURCE_DIR"/*.service >/dev/null 2>&1; then
    for unit in "$SOURCE_DIR"/*.service; do
        install -m 0644 "$unit" "$SYSTEMD_LIVE/$(basename "$unit")"
    done
    box_line "INFO: Services copied and permissions set"
else
    box_line "WARNING: No *.service files found in source"
fi

if ls "$SOURCE_DIR"/*.timer >/dev/null 2>&1; then
    for unit in "$SOURCE_DIR"/*.timer; do
        install -m 0644 "$unit" "$SYSTEMD_LIVE/$(basename "$unit")"
    done
    box_line "INFO: Timers copied and permissions set"
else
    box_line "WARNING: No *.timer files found in source"
fi
box_end

box_begin "Systemd Operations"
box_line "Reloading systemd daemon..."
systemctl daemon-reload
box_line "INFO: Daemon reloaded" "GREEN"

# Enable and start services, capturing output from systemctl enable
for service in "$SYSTEMD_LIVE"/sentrylab*.service; do
    if [ -f "$service" ]; then
        service_name=$(basename "$service")
        enable_out=$(systemctl enable "$service_name" 2>&1 || true)
        if [[ "$VERBOSE" == "true" && -n "$enable_out" ]]; then
            while IFS= read -r line; do
                [[ -n "$line" ]] && box_line "$line"
            done <<< "$enable_out"
        elif [[ -z "$enable_out" ]]; then
            box_line "INFO: $service_name already enabled" "LIGHTGRAY"
        fi
        start_out=$(systemctl start "$service_name" 2>&1 || true)
        if [[ "$VERBOSE" == "true" && -n "$start_out" ]]; then
            while IFS= read -r line; do
                [[ -n "$line" ]] && box_line "$line"
            done <<< "$start_out"
        fi
        box_value "Service" "$service_name enabled and started" "GREEN"
    fi
done

# Enable timers, capturing output from systemctl enable
for timer in "$SYSTEMD_LIVE"/sentrylab*.timer; do
    if [ -f "$timer" ]; then
        timer_name=$(basename "$timer")
        enable_out=$(systemctl enable "$timer_name" 2>&1 || true)
        if [[ "$VERBOSE" == "true" && -n "$enable_out" ]]; then
            while IFS= read -r line; do
                [[ -n "$line" ]] && box_line "$line"
            done <<< "$enable_out"
        elif [[ -z "$enable_out" ]]; then
            box_line "INFO: $timer_name already enabled" "LIGHTGRAY"
        fi
        start_out=$(systemctl start "$timer_name" 2>&1 || true)
        if [[ "$VERBOSE" == "true" && -n "$start_out" ]]; then
            while IFS= read -r line; do
                [[ -n "$line" ]] && box_line "$line"
            done <<< "$start_out"
        fi
        box_value "Timer" "$timer_name enabled and started" "GREEN"
    fi
done
box_end

echo
box_line "SentryLab services activated successfully" "GREEN"
box_line "Check status: systemctl status sentrylab*.service sentrylab*.timer" "CYAN"
echo
