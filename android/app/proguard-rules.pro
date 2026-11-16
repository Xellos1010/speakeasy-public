# OkHttp / Okio ship their own consumer rules. Keep our IME/tile/widget entry points (manifest-referenced, kept by AAPT).
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
# Tink (via androidx.security-crypto) references compile-only errorprone annotations.
-dontwarn com.google.errorprone.annotations.**
-dontwarn javax.annotation.**
