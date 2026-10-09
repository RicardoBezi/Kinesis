package dev.kinesis.client.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.kinesis.client.generated.model.PlanMode
import dev.kinesis.client.generated.model.SceneRef
import dev.kinesis.client.state.AnimationSelectionDraft

/**
 * The selection for an uploaded scene: armature, foot bone, frame range, plan mode and an
 * optional instruction. Defaults follow the canonical fixture (foot.L, frames 45-90).
 */
@Composable
fun NewRepairDialog(scene: SceneRef, onSubmit: (AnimationSelectionDraft) -> Unit, onDismiss: () -> Unit) {
    var armature by remember { mutableStateOf(scene.armatures.firstOrNull()?.name ?: "") }
    val bones = scene.armatures.firstOrNull { it.name == armature }?.bones.orEmpty().map { it.name }
    var bone by remember(armature) { mutableStateOf(bones.firstOrNull { it.startsWith("foot") } ?: bones.firstOrNull() ?: "") }
    var start by remember { mutableStateOf(maxOf(scene.frameStart, 45).toString()) }
    var end by remember { mutableStateOf(minOf(scene.frameEnd, 90).toString()) }
    var useModel by remember { mutableStateOf(true) }
    var instruction by remember { mutableStateOf("") }
    val valid = armature.isNotBlank() && bone.isNotBlank() &&
        (start.toIntOrNull() ?: Int.MAX_VALUE) <= (end.toIntOrNull() ?: Int.MIN_VALUE)

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("New repair") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Scene ${scene.sceneId}: frames ${scene.frameStart}–${scene.frameEnd} at ${scene.fps} fps, Blender ${scene.blenderVersion}")
                Picker("Armature", armature, scene.armatures.map { it.name }) { armature = it }
                Picker("Foot bone", bone, bones) { bone = it }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(start, { start = it.filter(Char::isDigit) }, label = { Text("First frame") }, modifier = Modifier.width(140.dp))
                    OutlinedTextField(end, { end = it.filter(Char::isDigit) }, label = { Text("Last frame") }, modifier = Modifier.width(140.dp))
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(useModel, { useModel = it })
                    Text("Let the model plan A/B (otherwise deterministic presets)")
                }
                OutlinedTextField(
                    instruction, { instruction = it.take(500) },
                    label = { Text("Instruction (optional), e.g. keep the heel planted") },
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        },
        confirmButton = {
            TextButton(
                enabled = valid,
                onClick = {
                    onSubmit(
                        AnimationSelectionDraft(
                            armature = armature,
                            targetBone = bone,
                            frameStart = start.toInt(),
                            frameEnd = end.toInt(),
                            planMode = if (useModel) PlanMode.AUTO else PlanMode.DETERMINISTIC,
                            instruction = instruction,
                        ),
                    )
                },
            ) { Text("Repair") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun Picker(label: String, value: String, options: List<String>, onPick: (String) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(label, Modifier.width(90.dp))
        OutlinedButton(onClick = { open = true }) { Text(value.ifBlank { "—" }) }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            options.forEach { option ->
                DropdownMenuItem(text = { Text(option) }, onClick = { onPick(option); open = false }, modifier = Modifier.padding(0.dp))
            }
        }
    }
}
