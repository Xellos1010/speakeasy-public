package com.speakeasy.android.data

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.speakeasy.android.BuildConfig
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/** Device token lives only in EncryptedSharedPreferences (AES-256, key in Android Keystore). */
class SecureStore(context: Context) {
    private val prefs: SharedPreferences = open(context)

    var token: String?
        get() = prefs.getString(K_TOKEN, null)
        set(v) { prefs.edit().apply { if (v == null) remove(K_TOKEN) else putString(K_TOKEN, v) }.apply() }

    val paired: Boolean get() = !token.isNullOrBlank()

    companion object {
        private const val FILE = "speakeasy_secure"
        private const val K_TOKEN = "device_token"

        private fun open(ctx: Context): SharedPreferences {
            fun build(): SharedPreferences {
                val key = MasterKey.Builder(ctx).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build()
                return EncryptedSharedPreferences.create(ctx, FILE, key,
                    EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                    EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM)
            }
            return try { build() } catch (e: Exception) {
                // Keystore/key mismatch (e.g. restored backup): reset the secure file; user re-pairs.
                Log.w("SpeakEasy", "secure prefs reset: ${e.javaClass.simpleName}")
                ctx.deleteSharedPreferences(FILE)
                build()
            }
        }
    }
}

/** Non-secret settings. */
class Settings(context: Context) {
    private val p = context.getSharedPreferences("speakeasy_settings", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = p.getString("server_url", null) ?: BuildConfig.DEFAULT_SERVER_URL
        set(v) = p.edit().putString("server_url", v.trim().trimEnd('/')).apply()

    var mode: String
        get() = p.getString("mode", "raw") ?: "raw"
        set(v) = p.edit().putString("mode", v).apply()

    var deviceName: String?
        get() = p.getString("device_name", null)
        set(v) = p.edit().putString("device_name", v).apply()

    /** Server-issued device id if pairing returned one, else a stable local UUID. */
    var deviceId: String
        get() = p.getString("device_id", null) ?: UUID.randomUUID().toString().also { deviceId = it }
        set(v) = p.edit().putString("device_id", v).apply()
}

data class HistoryEntry(
    val id: String,              // local id (== queue id)
    val utteranceId: String?,    // server id once transcribed
    val text: String,
    val rawText: String,
    val mode: String,
    val createdAt: Long,
    val status: String,          // done | queued | error
    val rewritten: Boolean = false,
    val edited: Boolean = false,
    val error: String? = null,
    val latencyMs: Long? = null,
) {
    fun toJson() = JSONObject().apply {
        put("id", id); put("utterance_id", utteranceId ?: JSONObject.NULL); put("text", text)
        put("raw_text", rawText); put("mode", mode); put("created_at", createdAt); put("status", status)
        put("rewritten", rewritten); put("edited", edited); put("error", error ?: JSONObject.NULL)
        put("latency_ms", latencyMs ?: JSONObject.NULL)
    }

    companion object {
        fun fromJson(o: JSONObject) = HistoryEntry(
            id = o.getString("id"),
            utteranceId = if (o.isNull("utterance_id")) null else o.optString("utterance_id"),
            text = o.optString("text"), rawText = o.optString("raw_text"), mode = o.optString("mode", "raw"),
            createdAt = o.optLong("created_at"), status = o.optString("status", "done"),
            rewritten = o.optBoolean("rewritten"), edited = o.optBoolean("edited"),
            error = if (o.isNull("error")) null else o.optString("error"),
            latencyMs = if (o.isNull("latency_ms")) null else o.optLong("latency_ms"),
        )
    }
}

/** Local dictation history (newest first), JSON file in app-private storage. */
class HistoryStore(private val file: File, private val cap: Int = 300) {
    @Synchronized
    fun all(): List<HistoryEntry> = try {
        if (!file.exists()) emptyList() else {
            val a = JSONArray(file.readText())
            (0 until a.length()).map { HistoryEntry.fromJson(a.getJSONObject(it)) }
        }
    } catch (_: Exception) { emptyList() }

    @Synchronized
    fun upsert(e: HistoryEntry) {
        val list = all().filterNot { it.id == e.id }.toMutableList()
        list.add(0, e)
        list.sortByDescending { it.createdAt }
        save(list.take(cap))
    }

    @Synchronized
    fun update(id: String, f: (HistoryEntry) -> HistoryEntry) {
        save(all().map { if (it.id == id) f(it) else it })
    }

    private fun save(list: List<HistoryEntry>) {
        val a = JSONArray(); list.forEach { a.put(it.toJson()) }
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(a.toString())
        if (!tmp.renameTo(file)) { file.delete(); tmp.renameTo(file) }
    }
}
