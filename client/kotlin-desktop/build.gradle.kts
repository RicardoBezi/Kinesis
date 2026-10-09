import org.jetbrains.compose.desktop.application.dsl.TargetFormat

plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.compose)
    alias(libs.plugins.openapi.generator)
}

group = "dev.kinesis"
version = "0.1.0"

kotlin {
    jvmToolchain(17)
}

// ADR 0008: the models are generated from the committed OpenAPI contract and never
// hand-copied. A backend schema change therefore shows up here as a compile error.
val openApiSpec = rootProject.layout.projectDirectory.file("../../docs/api/openapi.json")
val generatedDir = layout.buildDirectory.dir("generated/openapi")

openApiGenerate {
    generatorName.set("kotlin")
    inputSpec.set(openApiSpec.asFile.toURI().toString()) // URI form works on Windows too
    outputDir.set(generatedDir.get().asFile.absolutePath)
    packageName.set("dev.kinesis.client.generated")
    modelPackage.set("dev.kinesis.client.generated.model")
    globalProperties.set(mapOf("models" to "", "modelDocs" to "false", "modelTests" to "false"))
    // JSON numbers become Double (not BigDecimal), and free-form JSON becomes JsonElement.
    typeMappings.set(
        mapOf(
            "number" to "kotlin.Double",
            "AnyType" to "JsonElement",
        ),
    )
    importMappings.set(mapOf("JsonElement" to "kotlinx.serialization.json.JsonElement"))
    configOptions.set(
        mapOf(
            "serializationLibrary" to "kotlinx_serialization",
            "enumPropertyNaming" to "UPPERCASE",
            "dateLibrary" to "string",
            "sourceFolder" to "src/main/kotlin",
        ),
    )
}

sourceSets.main {
    kotlin.srcDir(generatedDir.map { it.dir("src/main/kotlin") })
}

// Generator quirk: number defaults are emitted with the BigDecimal template,
// `kotlin.Double("0.01")`, even when the type is mapped to Double. Rewrite them as literals.
tasks.named("openApiGenerate") {
    val outDir = generatedDir.get().asFile
    doLast {
        val bigDecimalDefault = Regex("""kotlin\.Double\("([^"]+)"\)""")
        outDir.walkTopDown().filter { it.extension == "kt" }.forEach { file ->
            val text = file.readText()
            val fixed = bigDecimalDefault.replace(text) { it.groupValues[1] }
            if (fixed != text) file.writeText(fixed)
        }
    }
}

tasks.named("compileKotlin") { dependsOn("openApiGenerate") }

dependencies {
    implementation(compose.desktop.currentOs)
    implementation(compose.material3)
    implementation(libs.ktor.client.core)
    implementation(libs.ktor.client.cio)
    implementation(libs.ktor.client.content.negotiation)
    implementation(libs.ktor.serialization.kotlinx.json)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.core)
    implementation(libs.kotlinx.coroutines.swing)

    testImplementation(kotlin("test"))
    testImplementation(libs.ktor.client.mock)
    testImplementation(libs.kotlinx.coroutines.test)
}

tasks.test {
    useJUnitPlatform()
}

compose.desktop {
    application {
        mainClass = "dev.kinesis.client.MainKt"
        nativeDistributions {
            targetFormats(TargetFormat.Msi, TargetFormat.Dmg, TargetFormat.Deb)
            packageName = "Kinesis Review"
            packageVersion = "1.0.0"
        }
    }
}

// Off-screen render of the review screen for a live job (see src/test/.../Screenshot.kt).
tasks.register<JavaExec>("screenshot") {
    group = "verification"
    description = "Render the review screen of a running job to a PNG"
    dependsOn("testClasses")
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass.set("dev.kinesis.client.ScreenshotKt")
    args = listOf(
        project.findProperty("job")?.toString() ?: error("-Pjob=<job_id> is required"),
        project.findProperty("out")?.toString() ?: layout.buildDirectory.file("screenshots/review.png").get().asFile.path,
        project.findProperty("api")?.toString() ?: "http://127.0.0.1:8000",
        project.findProperty("frame")?.toString() ?: "30",
    )
}
