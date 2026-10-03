package com.oneaxe.pocket.voicelab;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class Settings {
    private static final String ALIAS = "voice_lab_mobile_bearer_v2";
    private static final String PREFS = "voice_lab";
    private static final String DEFAULT_HOST = "rtx4090.nase-stairs.ts.net";
    private static final int DEFAULT_PORT = 8097;

    private static SharedPreferences preferences(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, 0);
        if (!prefs.getBoolean("tailnet_migrated_v2", false)) {
            prefs.edit().remove("url").remove("token").remove("iv")
                    .putBoolean("tailnet_migrated_v2", true).commit();
        }
        return prefs;
    }

    static String scheme(Context context) {
        return preferences(context).getString("scheme_v2", "https");
    }

    static String host(Context context) {
        return preferences(context).getString("host_v2", DEFAULT_HOST);
    }

    static int port(Context context) {
        return preferences(context).getInt("port_v2", DEFAULT_PORT);
    }

    static TailnetEndpoint endpoint(Context context) {
        return TailnetEndpoint.validate(scheme(context), host(context), port(context));
    }

    static String url(Context context) {
        return endpoint(context).baseUrl();
    }

    static void save(Context context, String scheme, String host, int port, String token) throws Exception {
        TailnetEndpoint endpoint = TailnetEndpoint.validate(scheme, host, port);
        SharedPreferences prefs = preferences(context);
        boolean sameEndpoint = endpoint.scheme.equals(prefs.getString("scheme_v2", "https")) &&
                endpoint.host.equals(prefs.getString("host_v2", DEFAULT_HOST)) &&
                endpoint.port == prefs.getInt("port_v2", DEFAULT_PORT);
        SharedPreferences.Editor editor = prefs.edit()
                .putString("scheme_v2", endpoint.scheme)
                .putString("host_v2", endpoint.host)
                .putInt("port_v2", endpoint.port);
        if (token == null || token.trim().isEmpty()) {
            if (!sameEndpoint) editor.remove("mobile_token_v2").remove("mobile_iv_v2");
            editor.apply();
            return;
        }
        SecretKey key = key();
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key);
        byte[] encrypted = cipher.doFinal(token.trim().getBytes(StandardCharsets.UTF_8));
        editor.putString("mobile_token_v2", Base64.encodeToString(encrypted, Base64.NO_WRAP))
                .putString("mobile_iv_v2", Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP))
                .apply();
    }

    static void clearToken(Context context) {
        preferences(context).edit().remove("mobile_token_v2").remove("mobile_iv_v2").apply();
    }

    static String token(Context context) throws Exception {
        SharedPreferences prefs = preferences(context);
        String encoded = prefs.getString("mobile_token_v2", null);
        String iv = prefs.getString("mobile_iv_v2", null);
        if (encoded == null || iv == null) return "";
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP)));
        return new String(cipher.doFinal(Base64.decode(encoded, Base64.NO_WRAP)), StandardCharsets.UTF_8);
    }

    private static SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (!store.containsAlias(ALIAS)) {
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
            generator.generateKey();
        }
        return (SecretKey) store.getKey(ALIAS, null);
    }

    private Settings() {}
}
