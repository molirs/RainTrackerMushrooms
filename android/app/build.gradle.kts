import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

val appVersion = providers.gradleProperty("appVersion").get()
val githubRepo = providers.gradleProperty("githubRepo").get()

// 1.2.3 -> 10203: cada versión publicada debe tener un versionCode mayor que la anterior
fun versionCodeOf(v: String): Int {
    val (major, minor, patch) = (v.split("-")[0].split(".").map { it.toInt() } + listOf(0, 0, 0)).take(3)
    return major * 10000 + minor * 100 + patch
}

// Firma de las versiones publicadas: keystore.properties en local, variables de entorno en GitHub Actions.
// Siempre la misma clave: Android no deja actualizar una app firmada con otra.
val signing = Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
fun signingValue(key: String, env: String): String? = signing.getProperty(key) ?: System.getenv(env)

android {
    namespace = "es.radarsetas"
    compileSdk = 36

    defaultConfig {
        applicationId = "es.radarsetas"
        minSdk = 26
        targetSdk = 36
        versionCode = versionCodeOf(appVersion)
        versionName = appVersion
        buildConfigField("String", "GITHUB_REPO", "\"$githubRepo\"")
        ndk {
            // arm64: cualquier móvil actual; x86_64: el emulador del PC
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    signingConfigs {
        create("release") {
            val store = signingValue("storeFile", "RELEASE_KEYSTORE")
            if (store != null) {
                storeFile = rootProject.file(store)
                storePassword = signingValue("storePassword", "RELEASE_KEYSTORE_PASSWORD")
                keyAlias = signingValue("keyAlias", "RELEASE_KEY_ALIAS")
                keyPassword = signingValue("keyPassword", "RELEASE_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release").takeIf { it.storeFile != null }
        }
    }

    buildFeatures { buildConfig = true }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    // El frontend es el mismo de la web: se empaqueta tal cual desde app/static
    sourceSets["main"].assets.srcDir("../../app/static")
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
}

// El paquete Python 'app' del proyecto, sin lo que solo usa el servidor
val pythonSrc = layout.buildDirectory.dir("python-src")
val copyPython by tasks.registering(Sync::class) {
    from("../../app") {
        include("**/*.py", "data/**")
        exclude("main.py", "launcher.py", "scheduler.py", "**/__pycache__/**")
    }
    into(pythonSrc.map { it.dir("app") })
}
tasks.matching { it.name.matches(Regex("merge.*PythonSources")) }.configureEach { dependsOn(copyPython) }

chaquopy {
    defaultConfig {
        version = "3.12"
        pip {
            install("requests")
            install("certifi")
            install("python-dotenv")
        }
        // Se leen como ficheros (provinces.geojson, certificados, cacert.pem de certifi)
        extractPackages("app", "certifi")
    }
    sourceSets {
        getByName("main") { srcDir(pythonSrc) }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
}
