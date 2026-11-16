import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Per-deployment settings are NOT in this file. Each is looked up by NAME, in this order:
// a Gradle property (-P or ~/.gradle/gradle.properties), android/local.properties (gitignored,
// see local.properties.example), then an environment variable. docs/PROFILE.md lists them.
//   speakeasy.serverUrl          default server URL baked into BuildConfig (empty = user pairs via QR)
//   speakeasy.keystore           release keystore path (default keystore/speakeasy-release.jks)
//   speakeasy.keyAlias           release key alias (default "speakeasy")
//   speakeasy.storePasswordEnv   NAME of the env var holding the keystore password (default SPK_KS_PW)
// The keystore password itself is never stored in any file; it is read from that env var.
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
fun setting(name: String, envName: String? = null): String =
    providers.gradleProperty(name).orNull
        ?: localProps.getProperty(name)
        ?: envName?.let { System.getenv(it) }
        ?: ""

val serverUrl = setting("speakeasy.serverUrl", "SPEAKEASY_SERVER_URL").trim().trimEnd('/')
val ksFile = rootProject.file(setting("speakeasy.keystore", "SPEAKEASY_KEYSTORE").ifBlank { "keystore/speakeasy-release.jks" })
val ksAlias = setting("speakeasy.keyAlias", "SPEAKEASY_KEY_ALIAS").ifBlank { "speakeasy" }
val ksPwEnvName = setting("speakeasy.storePasswordEnv").ifBlank { "SPK_KS_PW" }
val ksPw: String? = System.getenv(ksPwEnvName)

android {
    namespace = "com.speakeasy.android"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.speakeasy.android"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("String", "DEFAULT_SERVER_URL", "\"${serverUrl.replace("\\", "\\\\").replace("\"", "\\\"")}\"")
    }

    signingConfigs {
        if (ksFile.exists() && ksPw != null) {
            create("release") {
                storeFile = ksFile
                storePassword = ksPw
                keyAlias = ksAlias
                keyPassword = ksPw
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    testOptions { unitTests.isReturnDefaultValues = true }
    packaging { resources { excludes += "/META-INF/{AL2.0,LGPL2.1}" } }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2025.05.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.core:core-ktx:1.16.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.9.0")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.0")
    implementation("androidx.work:work-runtime-ktx:2.10.1")
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0") // QR pairing scan (on-device, no Google services)
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")

    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    testImplementation("org.json:json:20260814")

    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:core:1.6.1")
}
