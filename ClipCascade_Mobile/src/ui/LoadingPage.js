import { ActivityIndicator, Text, View } from 'react-native';

import { APP_NAME } from '../appConfig';
import styles from './styles';

export default function LoadingPage({ message }) {
  return (
    <View style={styles.loadingContainer}>
      <Text style={styles.appTitle}>{APP_NAME}</Text>
      <View style={styles.loadingBottomContainer}>
        <Text style={styles.loadingText}>{message}</Text>
        <ActivityIndicator size="large" />
      </View>
    </View>
  );
}
