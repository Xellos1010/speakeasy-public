package com.speakeasy.android.data

import org.json.JSONObject

/** Pairing info from the Mac's QR (contract v1 JSON payload) or a speakeasy://pair?code=&url= link. */
data class PairPayload(val code: String, val url: String?, val device: String?) {
    companion object {
        fun parse(raw: String?): PairPayload? {
            val s = raw?.trim() ?: return null
            if (s.startsWith("{")) return try {
                val o = JSONObject(s)
                if (o.optString("type") != "speakeasy-pair") return null
                val code = o.optString("code").filter { it.isDigit() }
                if (code.length != 6) null
                else PairPayload(code, o.optString("url").ifEmpty { null }, o.optString("device").ifEmpty { null })
            } catch (_: Exception) { null }
            if (s.startsWith("speakeasy://pair")) {
                val q = s.substringAfter('?', "").split('&').mapNotNull {
                    val i = it.indexOf('='); if (i < 0) null else it.substring(0, i) to java.net.URLDecoder.decode(it.substring(i + 1), "UTF-8")
                }.toMap()
                val code = q["code"]?.filter { it.isDigit() } ?: return null
                return if (code.length == 6) PairPayload(code, q["url"], q["device"]) else null
            }
            val digits = s.filter { it.isDigit() }
            return if (digits.length == 6 && s.length <= 8) PairPayload(digits, null, null) else null
        }
    }
}
