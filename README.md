This component is a test one and requires manual installation. Copy `audio_recognizer` folder to `/homeassistant/custom_components/`

## Audio recognition
```
action: audio_recognizer.recognize_file
data:
  entity_id: stt.groqcloud_whisper
  file_path: /media/test.wav
  language: en
```

## Interaction with Telegram
>[!CAUTION]
>One bot - one integration!

You can enable interaction with your TG bot in the settings and select the STT.
There are two events for data sent to the bot:
- `audio_recognizer_text_received` - for plain text
- `audio_recognizer_transcription` - for text from recognized audio



There's an option in the settings to receive the recognized text in the Telegram chat. This is useful for privately transcribing voice messages; simply forward the audio to the bot.

<img width="466" height="131" alt="image" src="https://github.com/user-attachments/assets/cec4d2bf-a540-476f-994b-8c4c0938c3f0" />



An example of working with an event to interact with **Assist**
```yaml
triggers:
  - event_type: audio_recognizer_text_received
    trigger: event
actions:
  - data:
      text: "{{ trigger.event.data.text }}"
      agent_id: conversation.llm
    action: conversation.process
    response_variable: answer
  - action: audio_recognizer.send_reply
    metadata: {}
    data:
      message: "{{answer.response.speech.plain.speech}}"
      chat_id: "{{ trigger.event.data.chat_id }}"
```
