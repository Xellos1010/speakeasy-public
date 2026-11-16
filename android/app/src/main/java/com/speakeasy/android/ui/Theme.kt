package com.speakeasy.android.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

object Foundry {
    val Ok = Color(0xFF2FBF71)
    val Warn = Color(0xFFF2A93B)
    val Err = Color(0xFFE5484D)
    val Ai = Color(0xFF9B7BFF)
}

private val Dark = darkColorScheme(
    primary = Color(0xFF5B95FF), onPrimary = Color(0xFF0A0F1C),
    secondary = Color(0xFF9B7BFF), background = Color(0xFF0F1115), onBackground = Color(0xFFE8EAF0),
    surface = Color(0xFF171A21), onSurface = Color(0xFFE8EAF0), surfaceVariant = Color(0xFF222632),
    onSurfaceVariant = Color(0xFFB4BAC8), outline = Color(0xFF3A4050), error = Foundry.Err,
)
private val Light = lightColorScheme(
    primary = Color(0xFF2563EB), onPrimary = Color.White,
    secondary = Color(0xFF6D4AE0), background = Color(0xFFF7F8FA), onBackground = Color(0xFF12151C),
    surface = Color.White, onSurface = Color(0xFF12151C), surfaceVariant = Color(0xFFEDEFF4),
    onSurfaceVariant = Color(0xFF4A5163), outline = Color(0xFFC9CEDA), error = Color(0xFFD12F35),
)

@Composable
fun SpeakEasyTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) Dark else Light, content = content)
}
