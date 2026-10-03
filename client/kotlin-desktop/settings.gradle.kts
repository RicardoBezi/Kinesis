pluginManagement {
    repositories {
        // Central first: the plugin portal only redirects there for these artifacts, and
        // resolving them through the portal was flaky in CI.
        mavenCentral()
        gradlePluginPortal()
        google()
    }
}

plugins {
    // Auto-provisions the JDK 17 toolchain on machines that only have an older JDK.
    id("org.gradle.toolchains.foojay-resolver-convention") version "1.0.0"
}

dependencyResolutionManagement {
    repositories {
        mavenCentral()
        google()
    }
}

rootProject.name = "kinesis-review"
