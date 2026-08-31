import { Linking, ScrollView, Text, TouchableOpacity, View } from 'react-native';

import notifee from '@notifee/react-native';

import styles from './styles';

/**
 * Static on-device setup instructions shown on the websocket page. Dumb
 * component: no props, no state.
 */
export default function InstructionsSection() {
  return (
    <View style={{ marginTop: 20, paddingHorizontal: 10 }}>
      <Text style={[styles.message, { fontWeight: 'bold', fontSize: 18 }]}>
        Instructions
      </Text>

      <View style={{ marginTop: 15 }}>
        <Text
          style={[
            styles.label,
            { fontWeight: 'bold', marginBottom: 5 },
          ]}
        >
          Clipboard Sharing on Android 10+:
        </Text>
        <Text style={styles.label}>
          On Android 10 and above, clipboard monitoring has been restricted
          for privacy reasons. To share clipboard content using ClipCascade:
        </Text>
        <View style={{ marginTop: 10, marginLeft: 15 }}>
          <Text style={styles.label}>
            1. Select the text, image, or file(s) you want to copy.
          </Text>
          <Text style={styles.label}>2. Tap 'Share', select 'ClipCascade'.</Text>
          <Text style={[styles.label, { marginLeft: 15 }]}>(or)</Text>
          <Text style={[styles.label, { marginLeft: 15 }]}>
            Tap 'ClipCascade' instead of 'Copy'.
          </Text>
        </View>
        <Text
          style={[
            styles.label,
            { marginTop: 5, fontSize: 15, fontStyle: 'italic' },
          ]}
        >
          There's also a workaround to enable clipboard sharing in the
          background. Scroll down for setup instructions.
        </Text>
      </View>

      <View style={{ marginTop: 20 }}>
        <Text
          style={[
            styles.label,
            { fontWeight: 'bold', marginBottom: 5 },
          ]}
        >
          Background Clipboard Reception:
        </Text>
        <Text style={styles.label}>
          ClipCascade automatically receives clipboard content in the
          background. No manual action is required to receive data.
        </Text>
      </View>

      <View style={{ marginTop: 20 }}>
        <Text
          style={[
            styles.label,
            { fontWeight: 'bold', marginBottom: 5 },
          ]}
        >
          Important Note:
        </Text>
        <Text style={styles.label}>
          To ensure uninterrupted performance, please disable battery
          optimization for ClipCascade. This will prevent the system from
          stopping the app when it's running in the foreground.
        </Text>
      </View>

      <TouchableOpacity
        style={[styles.loginButton, { backgroundColor: 'black' }]}
        onPress={async () => await notifee.openBatteryOptimizationSettings()}
      >
        <Text style={styles.loginButtonText}>
          Battery Optimization Settings
        </Text>
      </TouchableOpacity>
      <TouchableOpacity
        style={[styles.loginButton, { backgroundColor: 'black' }]}
        onPress={async () => await notifee.openPowerManagerSettings()}
      >
        <Text style={styles.loginButtonText}>
          Power Manager Settings
        </Text>
      </TouchableOpacity>

      {/* ADB Commands Section */}
      <View style={{ marginTop: 20 }}>
        <Text
          style={[
            styles.label,
            { fontWeight: 'bold', marginBottom: 5 },
          ]}
        >
          Automatic Clipboard Monitoring Setup:
        </Text>
        <Text style={styles.label}>
          On rooted/non-rooted devices, to enable automatic clipboard
          monitoring you need to execute these 3 ADB commands:
        </Text>
        <View style={{ marginTop: 10, marginLeft: 15 }}>
          <Text style={styles.label}>1. Enable the READ_LOGS permission:</Text>
          <Text
            selectable
            style={[
              styles.label,
              { fontWeight: 'bold', marginLeft: 15 },
            ]}
          >
            {`> adb -d shell pm grant com.clipcascade android.permission.READ_LOGS`}
          </Text>

          <Text style={styles.label}>
            2. Allow "Drawing over other apps", also accessible from Settings:
          </Text>
          <Text
            selectable
            style={[
              styles.label,
              { fontWeight: 'bold', marginLeft: 15 },
            ]}
          >
            {`> adb -d shell appops set com.clipcascade SYSTEM_ALERT_WINDOW allow`}
          </Text>

          <Text style={styles.label}>
            3. Kill the app for the new permissions to take effect:
          </Text>
          <Text
            selectable
            style={[
              styles.label,
              { fontWeight: 'bold', marginLeft: 15 },
            ]}
          >
            {`> adb -d shell am force-stop com.clipcascade`}
          </Text>
        </View>
      </View>
    </View>
  );
}
