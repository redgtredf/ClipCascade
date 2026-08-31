import { Linking, Text, TouchableOpacity, View } from 'react-native';

import { GITHUB_URL, HELP_URL } from '../appConfig';
import styles from './styles';

/**
 * Shared page footer. `homepageUrl` adds a HOMEPAGE link when provided
 * (the websocket page passes the user's server URL).
 */
export default function Footer({ donateUrl = null, homepageUrl = null }) {
  return (
    <View style={styles.footerContainer}>
      <TouchableOpacity
        style={styles.spacing}
        onPress={() => Linking.openURL(GITHUB_URL)}
      >
        <Text style={styles.footerText}>GITHUB</Text>
      </TouchableOpacity>
      <TouchableOpacity
        style={styles.spacing}
        onPress={() => Linking.openURL(HELP_URL)}
      >
        <Text style={styles.footerText}>HELP</Text>
      </TouchableOpacity>
      {donateUrl && (
        <TouchableOpacity
          style={styles.spacing}
          onPress={() => Linking.openURL(donateUrl)}
        >
          <Text style={styles.footerText}>DONATE</Text>
        </TouchableOpacity>
      )}
      {homepageUrl && (
        <TouchableOpacity
          style={styles.spacing}
          onPress={() => Linking.openURL(homepageUrl)}
        >
          <Text style={styles.footerText}>HOMEPAGE</Text>
        </TouchableOpacity>
      )}
    </View>
  );
}
