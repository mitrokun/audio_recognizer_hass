## Copy, Install and Use
```
action: audio_recognizer.recognize_file
data:
  entity_id: stt.groqcloud_whisper
  file_path: /media/test.wav
  language: en
```

### Interaction with Telegram
>[!CAUTION]
>One bot - one integration!

You can enable interaction with your TG bot in the settings and select the STT. After that, audio messages sent to the bot will be decoded accordingly.
This action also generates an event in HA, allowing you to create automations. For example, voice control is possible:
```yaml
triggers:
  - event_type: audio_recognizer_transcription
    trigger: event
actions:
  - data:
      text: "{{ trigger.event.data.text }}"
    action: conversation.process
    response_variable: answer
  - action: audio_recognizer.send_reply
    metadata: {}
    data:
      message: "{{answer.response.speech.plain.speech}}"
      chat_id: "{{ trigger.event.data.chat_id }}"
```
