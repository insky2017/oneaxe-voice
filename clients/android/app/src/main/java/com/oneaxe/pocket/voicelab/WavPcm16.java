package com.oneaxe.pocket.voicelab;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

final class WavPcm16 {
    static byte[] readMono16k(File file) throws IOException {
        if (file.length() < 44 || file.length() > 12 * 1024 * 1024) throw new IOException("invalid WAV size");
        byte[] bytes = new byte[(int) file.length()];
        try (FileInputStream input = new FileInputStream(file)) {
            int offset = 0;
            while (offset < bytes.length) {
                int size = input.read(bytes, offset, bytes.length - offset);
                if (size < 0) throw new IOException("incomplete WAV");
                offset += size;
            }
        }
        ByteBuffer buffer = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        if (buffer.getInt(0) != 0x46464952 || buffer.getInt(8) != 0x45564157) throw new IOException("not RIFF/WAVE");
        boolean formatValid = false;
        int dataAt = -1, dataLength = -1;
        for (int offset = 12; offset + 8 <= bytes.length;) {
            int id = buffer.getInt(offset);
            long size = Integer.toUnsignedLong(buffer.getInt(offset + 4));
            long end = (long) offset + 8 + size;
            if (end > bytes.length) throw new IOException("truncated WAV chunk");
            if (id == 0x20746d66 && size >= 16) {
                formatValid = buffer.getShort(offset + 8) == 1 && buffer.getShort(offset + 10) == 1 &&
                        buffer.getInt(offset + 12) == 16000 && buffer.getShort(offset + 22) == 16;
            }
            if (id == 0x61746164) { dataAt = offset + 8; dataLength = (int) size; }
            offset = (int) (end + (size & 1));
        }
        if (!formatValid || dataAt < 0 || dataLength < 3200 || dataLength > 16000 * 2 * 60) {
            throw new IOException("WAV must be PCM16 mono 16 kHz, 0.1-60 seconds");
        }
        byte[] pcm = new byte[dataLength];
        System.arraycopy(bytes, dataAt, pcm, 0, dataLength);
        return pcm;
    }

    private WavPcm16() {}
}
