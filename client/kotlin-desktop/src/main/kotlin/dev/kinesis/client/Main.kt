package dev.kinesis.client

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Window
import androidx.compose.ui.window.application

/**
 * Kinesis review client. Phase 0 is a window stub. The review UI arrives in Phase 5:
 * job list, synchronized Original/A/B playback, metrics, and the decision.
 */
fun main() = application {
    Window(onCloseRequest = ::exitApplication, title = "Kinesis Review") {
        App()
    }
}

@Composable
fun App() {
    MaterialTheme {
        Surface(modifier = Modifier.fillMaxSize()) {
            Column(modifier = Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Kinesis Review", style = MaterialTheme.typography.headlineMedium)
                Text("Phase 0: contracts frozen. Review UI lands in Phase 5.")
            }
        }
    }
}
