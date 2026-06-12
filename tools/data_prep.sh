#!/bin/bash

# data_prep.sh
# Converts all audio files in a directory to 16 kHz mono PCM WAV, then optionally
# splits them into 1-second clips placed in a clips/ subfolder.
# Supported formats: wav, mp3, m4a, ogg, flac, aac, opus, wma, mkv, mp4, webm
# Usage: ./data_prep.sh [directory]
#   directory: optional, defaults to current directory

RAW_DIR="${1:-.}"

# Detect mangled Windows paths — backslashes get eaten by bash when unquoted.
# E.g. C:\Users\foo becomes C:Usersfoo — unrecoverable.
if echo "$RAW_DIR" | grep -qE '^[a-zA-Z]:[A-Za-z0-9_]'; then
    echo "ERROR: The path appears to be a Windows path whose backslashes were stripped by bash." >&2
    echo "       You passed: $1" >&2
    echo "       Script saw: $RAW_DIR" >&2
    echo "" >&2
    echo "Fix: quote the path or use forward slashes:" >&2
    echo "  ./data_prep.sh \"C:\\Users\\user\\Videos\\.elio\"" >&2
    echo "  ./data_prep.sh C:/Users/user/Videos/.elio" >&2
    exit 1
fi

# Convert Windows-style paths (C:\... or C:/...) to WSL Linux paths (/mnt/c/...)
if echo "$RAW_DIR" | grep -qEi '^[a-zA-Z]:[/\\]'; then
    DRIVE_LETTER=$(echo "$RAW_DIR" | cut -c1 | tr '[:upper:]' '[:lower:]')
    # Strip the drive letter + colon + leading slash/backslash, then normalize slashes
    WIN_PATH=$(echo "$RAW_DIR" | sed 's/^[a-zA-Z]:[\/\\]//' | sed 's/\\/\//g')
    TARGET_DIR="/mnt/$DRIVE_LETTER/$WIN_PATH"
else
    # Normalize any remaining backslashes to forward slashes (for mixed-style input)
    TARGET_DIR=$(echo "$RAW_DIR" | sed 's/\\/\//g')
fi
CLIPS_DIR="$TARGET_DIR/clips"
RAW_BACKUP_DIR="$TARGET_DIR/raw"

shopt -s nullglob
AUDIO_EXTENSIONS=(wav mp3 m4a ogg flac aac opus wma mkv mp4 webm)
AUDIO_FILES=()
for ext in "${AUDIO_EXTENSIONS[@]}"; do
    AUDIO_FILES+=("$TARGET_DIR"/*."$ext")
done

if [ ${#AUDIO_FILES[@]} -eq 0 ]; then
    echo "No audio files found in '$TARGET_DIR'"
    exit 1
fi

echo "Found ${#AUDIO_FILES[@]} audio file(s)."
echo "-------------------------------------------"

# --------------------------------------------------
# Step 0: Convert all audio files to 16 kHz mono PCM WAV
# --------------------------------------------------
echo ""
echo "[0/5] Converting to 16 kHz mono PCM WAV..."
mkdir -p "$RAW_BACKUP_DIR"
for f in "${AUDIO_FILES[@]}"; do
    filename=$(basename "$f")
    base="${filename%.*}"   # strip any extension
    echo "      Converting: $filename"
    ffmpeg -v error -i "$f" -ar 16000 -ac 1 -c:a pcm_s16le "$TARGET_DIR/converted_${base}.wav"
done

# Move original files to raw/ backup
echo "      Moving originals to raw/..."
for f in "${AUDIO_FILES[@]}"; do
    mv "$f" "$RAW_BACKUP_DIR/"
done

# Rename converted files to clean names (remove "converted_" prefix)
for f in "$TARGET_DIR"/converted_*.wav; do
    mv "$f" "$TARGET_DIR/${f##*/converted_}"
done

# Populate WAV_FILES with the converted files for the rest of the pipeline
WAV_FILES=("$TARGET_DIR"/*.wav)
echo "      Done."
echo "-------------------------------------------"

# Ask if the user also wants to split into 1-second clips
echo ""
read -p "Split converted files into 1-second clips? [y/N] " SPLIT_ANSWER
case "$SPLIT_ANSWER" in
    [yY]|[yY][eE][sS])
        echo "Proceeding with splitting..."
        mkdir -p "$CLIPS_DIR"
        ;;
    *)
        echo "Skipping split. Converted files are in: $TARGET_DIR"
        echo "Originals backed up to: $RAW_BACKUP_DIR"
        exit 0
        ;;
esac

for f in "${WAV_FILES[@]}"; do
    filename=$(basename "$f")
    base="${filename%.wav}"

    echo ""
    echo "[1/5] Getting duration: $filename"
    duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
    echo "      Duration: ${duration}s"

    echo "[2/5] Padding to next whole second..."
    # Ceiling: if duration is already a whole number, keep it; otherwise round up
    rounded=$(echo "$duration" | awk '{print int($1) == $1 ? int($1) : int($1)+1}')
    echo "      Padding to: ${rounded}s"
    padded_file="$TARGET_DIR/${base}_padded.wav"
    ffmpeg -v error -i "$f" -af "apad=pad_dur=1" -t "$rounded" "$padded_file" -y

    echo "[3/5] Splitting into 1-second clips..."
    ffmpeg -v error -i "$padded_file" -f segment -segment_time 1 "$CLIPS_DIR/${base}.s%d.wav"
    rm "$padded_file"

    echo "[4/5] Fixing any short clips..."
    fixed=0
    for clip in "$CLIPS_DIR/${base}".s*.wav; do
        clip_dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$clip")
        # Use awk to check if duration is less than 0.999s (handles floating point variations)
        is_short=$(echo "$clip_dur" | awk '{print ($1 < 0.999) ? "1" : "0"}')
        if [ "$is_short" = "1" ]; then
            fixed_file="${clip%.wav}_fixed.wav"
            ffmpeg -v error -i "$clip" -af "apad=pad_dur=1" -t 1 "$fixed_file" -y
            # NTFS-safe replace: delete original first, then move
            rm "$clip" && mv "$fixed_file" "$clip"
            ((fixed++))
        fi
    done
    echo "      Fixed $fixed short clip(s)."

    echo "Done: $filename"
done

echo ""
echo "-------------------------------------------"
echo "All files processed. Clips saved to: $CLIPS_DIR"