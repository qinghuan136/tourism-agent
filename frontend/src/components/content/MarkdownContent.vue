<script setup lang="ts">
import { computed } from 'vue'

import { renderMarkdown } from '@/utils/markdown'

const props = defineProps<{
  content: string
}>()

const renderedHtml = computed(() => renderMarkdown(props.content))
</script>

<template>
  <!-- 仅注入 renderMarkdown 经 DOMPurify 清洗后的 HTML。 -->
  <div
    class="markdown-content"
    v-html="renderedHtml"
  />
</template>
