<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppHeader from '@/components/shell/AppHeader.vue'
import TripWorkspaceLayout from '@/components/shell/TripWorkspaceLayout.vue'
import { useServiceStatusStore } from '@/stores/service-status'
import { useTripWorkspaceStore } from '@/stores/trip-workspace'

const route = useRoute()
const router = useRouter()
const serviceStatus = useServiceStatusStore()
const workspace = useTripWorkspaceStore()

const tripId = computed(() => String(route.params.tripId))
const tripLabel = computed(() => {
  const value = tripId.value
  return value.length > 13 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value
})

onMounted(() => {
  void serviceStatus.check()
})

watch(
  tripId,
  (value) => {
    void workspace.load(value)
  },
  { immediate: true },
)

function leaveTrip(): void {
  workspace.leaveTrip()
  void router.push('/')
}
</script>

<template>
  <div class="app-shell">
    <AppHeader
      :service-status="serviceStatus.status"
      :trip-label="tripLabel"
    />
    <TripWorkspaceLayout
      :status="workspace.status"
      :history-status="workspace.historyStatus"
      :bootstrap="workspace.bootstrap"
      :run-status="workspace.runStatus"
      :run-feedback="workspace.runFeedback"
      :run-issue="workspace.runIssue"
      :candidate-itinerary="workspace.candidateItinerary"
      :live-messages="workspace.liveMessages"
      :task-results="workspace.taskResults"
      :is-cancelling="workspace.isCancelling"
      @retry="workspace.load(tripId)"
      @load-earlier="workspace.loadEarlier()"
      @send-message="workspace.sendMessage"
      @confirm-candidate="workspace.confirmCandidate"
      @cancel-run="workspace.cancelRun"
      @retry-same="workspace.retryPendingRequest('same')"
      @retry-new="workspace.retryPendingRequest('new')"
      @restore-candidate="workspace.restoreCandidateConfirmation"
      @leave-trip="leaveTrip"
    />
  </div>
</template>
