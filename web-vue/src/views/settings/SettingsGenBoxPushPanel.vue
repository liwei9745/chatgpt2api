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

    <FormSection title="自动推送" subtitle="默认每周一次。系统会重叠扫描最近图片，晚到的图片也不会被悄悄遗漏。源图始终保留。">
      <div class="settings-check-item">
        <Checkbox v-model="scheduleForm.enabled">每周自动推送新图片</Checkbox>
      </div>
      <div class="grid gap-4 md:grid-cols-2">
        <FormField label="执行日">
          <select v-model.number="scheduleForm.weekday" class="schedule-select">
            <option v-for="day in weekdays" :key="day.value" :value="day.value">{{ day.label }}</option>
          </select>
        </FormField>
        <FormField label="执行时间"><Input v-model="scheduleForm.time" type="time" block /></FormField>
        <FormField label="最早日期（可选）"><Input v-model="scheduleForm.start_date" type="date" block /></FormField>
        <FormField label="截止日期（可选）"><Input v-model="scheduleForm.end_date" type="date" block /></FormField>
      </div>
      <div class="flex flex-wrap gap-2">
        <Button size="sm" variant="primary" :disabled="isScheduleSaving" @click="saveSchedule">{{ isScheduleSaving ? '保存中...' : '保存自动推送' }}</Button>
        <Button size="sm" variant="outline" :disabled="isScheduleRunning" @click="runScheduleNow">{{ isScheduleRunning ? '扫描中...' : '现在扫描一次' }}</Button>
      </div>
      <StateBlock v-if="scheduleMessage" :title="scheduleTone === 'success' ? '自动推送已准备好' : '需要处理'" :description="scheduleMessage" />
    </FormSection>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Button, Checkbox, FormField, FormSection, Input } from 'nanocat-ui'
import StateBlock from '@/components/ai/StateBlock.vue'
import { genboxPushApi } from '@/api/genboxPush'

const form = reactive({ enabled: false, base_url: '', source_id: '', push_key: '', timeout_secs: 20 })
const scheduleForm = reactive({ enabled: false, weekday: 0, time: '09:00', start_date: '', end_date: '' })
const weekdays = [
  { value: 0, label: '星期一' }, { value: 1, label: '星期二' }, { value: 2, label: '星期三' },
  { value: 3, label: '星期四' }, { value: 4, label: '星期五' }, { value: 5, label: '星期六' }, { value: 6, label: '星期日' },
]
const hasPushKey = ref(false)
const isSaving = ref(false)
const isProbing = ref(false)
const message = ref('')
const messageTone = ref<'success' | 'error'>('success')
const scheduleMessage = ref('')
const scheduleTone = ref<'success' | 'error'>('success')
const isScheduleSaving = ref(false)
const isScheduleRunning = ref(false)
const keyPlaceholder = computed(() => hasPushKey.value ? '密钥已保存；留空则不修改' : '从 GenBox 获取的推送密钥')

async function load() {
  try {
    const [response, scheduleResponse] = await Promise.all([genboxPushApi.getSettings(), genboxPushApi.getSchedule()])
    form.enabled = response.settings.enabled
    form.base_url = response.settings.base_url
    form.source_id = response.settings.source_id
    form.push_key = ''
    form.timeout_secs = response.settings.timeout_secs
    hasPushKey.value = response.settings.has_push_key
    Object.assign(scheduleForm, {
      enabled: scheduleResponse.schedule.enabled,
      weekday: scheduleResponse.schedule.weekday,
      time: scheduleResponse.schedule.time,
      start_date: scheduleResponse.schedule.start_date,
      end_date: scheduleResponse.schedule.end_date,
    })
  } catch (error: any) {
    messageTone.value = 'error'
    message.value = error?.message || '无法读取 GenBox 推送设置'
  }
}

function setScheduleMessage(schedule: { queued: number; succeeded: number; failed: number; last_error: string }, success: string) {
  scheduleTone.value = schedule.last_error ? 'error' : 'success'
  scheduleMessage.value = schedule.last_error || `${success} 已完成 ${schedule.succeeded} 张，等待中 ${schedule.queued} 张，失败 ${schedule.failed} 张；源图仍保留。`
}

async function saveSchedule() {
  isScheduleSaving.value = true
  scheduleMessage.value = ''
  try {
    const response = await genboxPushApi.updateSchedule({ ...scheduleForm })
    setScheduleMessage(response.schedule, response.schedule.enabled ? '每周自动推送已保存。' : '自动推送已关闭。')
  } catch (error: any) {
    scheduleTone.value = 'error'
    scheduleMessage.value = error?.message || '保存自动推送设置失败'
  } finally {
    isScheduleSaving.value = false
  }
}

async function runScheduleNow() {
  isScheduleRunning.value = true
  scheduleMessage.value = ''
  try {
    const response = await genboxPushApi.runScheduleNow()
    setScheduleMessage(response.schedule, '本次扫描已加入可恢复的推送任务。')
  } catch (error: any) {
    scheduleTone.value = 'error'
    scheduleMessage.value = error?.message || '无法开始本次自动推送扫描'
  } finally {
    isScheduleRunning.value = false
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
.schedule-select { width: 100%; min-height: 40px; border: 1px solid hsl(var(--border)); border-radius: 6px; background: hsl(var(--background)); padding: 0 10px; color: hsl(var(--foreground)); }
</style>
