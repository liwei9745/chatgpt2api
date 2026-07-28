<template>
  <div class="space-y-4">
    <FormSection title="推送到 GenBox" subtitle="把这里保存的图片发送到你已确认的 GenBox。源图始终保留。">
      <div class="settings-check-item">
        <Checkbox v-model="form.enabled">启用 GenBox 推送</Checkbox>
      </div>
      <div class="grid gap-4 md:grid-cols-2">
        <FormField label="GenBox 地址"><Input v-model.trim="form.base_url" block placeholder="https://genbox.example" /></FormField>
        <FormField label="来源标识"><Input v-model.trim="form.source_id" block placeholder="chatgpt2api-dev" /></FormField>
        <FormField label="推送密钥"><Input v-model="form.push_key" type="password" block :placeholder="keyPlaceholder" /></FormField>
        <FormField label="连接等待（秒）"><Input v-model="form.timeout_secs" type="number" min="5" max="120" block /></FormField>
      </div>
      <div class="flex flex-wrap gap-2">
        <Button size="sm" variant="primary" :disabled="isSaving" @click="save">{{ isSaving ? '保存中...' : '保存连接设置' }}</Button>
        <Button size="sm" variant="outline" :disabled="isSaving || isProbing" @click="probe">{{ isProbing ? '检查中...' : '检查 GenBox' }}</Button>
      </div>
      <StateBlock v-if="message" :title="messageTone === 'success' ? '可以继续' : '需要处理'" :description="message" />
    </FormSection>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Button, Checkbox, FormField, FormSection, Input } from 'nanocat-ui'
import StateBlock from '@/components/ai/StateBlock.vue'
import { genboxPushApi } from '@/api/genboxPush'

const form = reactive({ enabled: false, base_url: '', source_id: '', push_key: '', timeout_secs: 20 })
const hasPushKey = ref(false)
const isSaving = ref(false)
const isProbing = ref(false)
const message = ref('')
const messageTone = ref<'success' | 'error'>('success')
const keyPlaceholder = computed(() => hasPushKey.value ? '密钥已保存；留空则不修改' : '从 GenBox 获取的推送密钥')

async function load() {
  try {
    const response = await genboxPushApi.getSettings()
    form.enabled = response.settings.enabled
    form.base_url = response.settings.base_url
    form.source_id = response.settings.source_id
    form.push_key = ''
    form.timeout_secs = response.settings.timeout_secs
    hasPushKey.value = response.settings.has_push_key
  } catch (error: any) {
    messageTone.value = 'error'
    message.value = error?.message || '无法读取 GenBox 推送设置'
  }
}

async function save() {
  isSaving.value = true
  message.value = ''
  try {
    const response = await genboxPushApi.updateSettings({ ...form, push_key: form.push_key || undefined })
    hasPushKey.value = response.settings.has_push_key
    form.push_key = ''
    messageTone.value = 'success'
    message.value = '连接设置已保存。检查成功后，可到图片管理推送一张图片。'
  } catch (error: any) {
    messageTone.value = 'error'
    message.value = error?.message || '保存 GenBox 推送设置失败'
  } finally {
    isSaving.value = false
  }
}

async function probe() {
  isProbing.value = true
  message.value = ''
  try {
    const response = await genboxPushApi.probe()
    messageTone.value = 'success'
    message.value = `GenBox 已准备好接收图片（协议 ${response.result.contract_version}）。`
  } catch (error: any) {
    messageTone.value = 'error'
    message.value = error?.message || 'GenBox 检查失败'
  } finally {
    isProbing.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.settings-check-item { display: flex; min-height: 42px; align-items: center; padding: 0 12px; border: 1px solid hsl(var(--border)); border-radius: 8px; background: hsl(var(--background) / 0.72); }
</style>
