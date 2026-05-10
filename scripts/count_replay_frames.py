"""Count frame records in a .replay v2 file.

BSV v2 layout (per input/bsv/bsvmovie.{c,h}):
  - Header: 40 bytes (10 uint32, includes magic, vsn, crc, state_size,
    identifier(8), frame_count, block_size, superblock_size, ckpt_config)
  - compression byte (1) + encoding byte (1)
  - Initial checkpoint: 4 (size) + 4 (encoded_size) + 4 (compressed_size) + payload
  - Per frame:
      backref (4) + key_count(1) + key_events(12*N) + input_count(2)
      + input_events(8*N) + frame_token(1)
      + if frame_token == REPLAY_TOKEN_CHECKPOINT2_FRAME (3):
          compression(1) + encoding(1) + 4 + 4 + 4 + payload

Returns count of frame records actually parseable from the input track.
If this is < header frame_count, recording is truncated.
"""
import struct
import sys
from pathlib import Path

REPLAY_TOKEN_INVALID = ord("\0")  # 0
REPLAY_TOKEN_REGULAR_FRAME = ord("f")  # 102
REPLAY_TOKEN_CHECKPOINT_FRAME = ord("c")  # 99 (v0/v1 legacy)
REPLAY_TOKEN_CHECKPOINT2_FRAME = ord("C")  # 67 (v2)

KEY_EVENT_SIZE = 12  # bsv_key_data_t size; verify
INPUT_EVENT_SIZE = 8  # bsv_input_data_t size; verify


def parse(path: Path) -> dict:
    data = path.read_bytes()
    print(f"file: {path.name}  total: {len(data)} bytes")
    header = struct.unpack_from("<10I", data, 0)
    magic, vsn, crc, state_size, id_lo, id_hi, frame_count, block_size, sb_size, ckpt_cfg = header
    print(f"  magic=0x{magic:08X} vsn={vsn} crc=0x{crc:08X} state_size={state_size}")
    print(f"  frame_count_header={frame_count} block_size={block_size} sb_size={sb_size}")
    print(f"  ckpt_cfg=0x{ckpt_cfg:08X}  commit_interval={ckpt_cfg >> 24} commit_threshold={(ckpt_cfg >> 16) & 0xFF} compression={(ckpt_cfg >> 8) & 0xFF}")

    p = 40
    # initial checkpoint header bytes (compression + encoding) are written
    # by bsv_movie_reset_recording at line 358-359
    init_compression = data[p]; p += 1
    init_encoding = data[p]; p += 1
    print(f"  initial ckpt: compression={init_compression} encoding={init_encoding}")

    # initial checkpoint payload: size(4) + encoded_size(4) + compressed_size(4) + payload
    init_unc_size, init_enc_size, init_comp_size = struct.unpack_from("<3I", data, p)
    p += 12
    print(f"  initial ckpt: unc_size={init_unc_size} enc_size={init_enc_size} comp_size={init_comp_size}")
    print(f"  skipping initial payload of {init_comp_size} bytes (file pos before frames: {p} -> {p + init_comp_size})")
    p += init_comp_size

    # Now we should be at the first frame record.
    print(f"  first frame at offset {p}; remaining bytes: {len(data) - p}")

    frames = 0
    ckpt_frames = 0
    while p < len(data):
        if p + 4 > len(data):
            print(f"  EOF reading backref at frame {frames} (offset {p})")
            break
        backref = struct.unpack_from("<I", data, p)[0]; p += 4
        if p + 1 > len(data):
            print(f"  EOF reading key_count at frame {frames} (offset {p})")
            break
        key_count = data[p]; p += 1
        keys_size = key_count * KEY_EVENT_SIZE
        if p + keys_size > len(data):
            print(f"  EOF reading {key_count} key events at frame {frames}")
            break
        p += keys_size
        if p + 2 > len(data):
            print(f"  EOF reading input_count at frame {frames}")
            break
        input_count = struct.unpack_from("<H", data, p)[0]; p += 2
        inputs_size = input_count * INPUT_EVENT_SIZE
        if p + inputs_size > len(data):
            print(f"  EOF reading {input_count} input events at frame {frames}")
            break
        p += inputs_size
        if p + 1 > len(data):
            print(f"  EOF reading frame_token at frame {frames}")
            break
        token = data[p]; p += 1
        if token == REPLAY_TOKEN_CHECKPOINT2_FRAME:
            ckpt_frames += 1
            if p + 2 > len(data):
                print(f"  EOF reading checkpoint compression+encoding at frame {frames}")
                break
            cp_comp = data[p]; p += 1
            cp_enc = data[p]; p += 1
            if p + 12 > len(data):
                print(f"  EOF reading checkpoint sizes at frame {frames}")
                break
            unc_size, enc_size, comp_size = struct.unpack_from("<3I", data, p)
            p += 12
            if p + comp_size > len(data):
                print(f"  EOF reading checkpoint payload (size={comp_size}) at frame {frames}")
                break
            p += comp_size
        elif token != REPLAY_TOKEN_REGULAR_FRAME:
            print(f"  unexpected token {token} at frame {frames}, offset {p-1}")
            break
        frames += 1

    print(f"  parsed {frames} frame records ({ckpt_frames} checkpoint frames, rest regular)")
    print(f"  header says frame_count={frame_count}, parsed={frames}, {'MATCH' if frames == frame_count else 'MISMATCH (truncated)'}")
    return {"header_frame_count": frame_count, "parsed_frames": frames, "checkpoint_frames": ckpt_frames}


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        parse(Path(arg))
        print()
