export const chatResources = {
  es: {
    chat: {
      conversation: "Conversación",
      newConversation: "Nueva conversación",
      fixedDate: "28 de agosto de 2026",
      toolsCount: "Herramientas · {{count}}",
      toolStatus: { completed: "Listo", failed: "Error", running: "En curso" },
      delivery: { unconfirmed: "Entrega sin confirmar", delivered: "Entregado", sending: "Enviando" },
      userMessage: "Tu mensaje",
      deliveryWarning: "No se confirmó la entrega; no se reenviará automáticamente.",
      assistantResponse: "Respuesta de {{agent}}",
      streaming: "En curso",
      messagePlaceholder: "Mensaje a {{agent}}…",
      offlineDraft: "Borrador offline",
      stop: "Detener",
      running: "En ejecución",
      sendMessage: "Enviar mensaje",
      attachments: { add: "Agregar al chat", menu: "Opciones para agregar", image: "Imagen", imageHint: "JPG, PNG, WebP o GIF", file: "Archivo", fileHint: "PDF, documentos, hojas, texto o código", selected: "Archivos seleccionados", attached: "Archivos adjuntos", remove: "Quitar {{name}}", errors: { tooMany: "Puedes adjuntar hasta 5 archivos por mensaje.", tooLarge: "Cada archivo debe pesar como máximo 8 MB.", tooMuchTotal: "Los adjuntos deben pesar como máximo 12 MB en total.", unsupported: "Ese tipo de imagen no es compatible." } },
      offlineDraftNote: "El borrador queda en este dispositivo y no se enviará al recuperar la conexión.",
      disclaimer: "Hermes puede cometer errores. Verifica información importante.",
      agent: "Agente",
      yourAgent: "tu agente",
      thisAgent: "Este agente",
      startWithAgent: "Inicia una conversación con {{agent}}",
      createWithAgent: "Crea un chat con {{agent}}",
      readOnlyAgent: "{{agent}} está en modo solo lectura",
      sessionIsolation: "El contexto de esta sesión permanecerá aislado del resto de agentes.",
      startInWorkspace: "Inicia una conversación dentro de este workspace.",
      readOnlyDescription: "La protección actual no permite crear conversaciones ni enviar mensajes. Selecciona el entorno de pruebas para escribir.",
      creating: "Creando…",
      newChat: "Nuevo chat",
      waitingForResponse: "{{agent}} espera tu respuesta",
      typing: "{{agent}} está escribiendo",
      activity: {
        show: "Mostrar actividad de {{agent}}",
        hide: "Ocultar actividad de {{agent}}",
        label: "Actividad de {{agent}}",
        analyzing: "Analizando la solicitud",
        composing: "Redactando la respuesta",
        tool: "Herramienta",
        delegation: "Trabajo delegado"
      },
      toolEvidence: {
        show: "Mostrar historial de herramientas de {{agent}}",
        hide: "Ocultar historial de herramientas de {{agent}}",
        label: "Historial de herramientas de {{agent}}",
        showHint: "Pulsa para ver el historial",
        hideHint: "Pulsa para ocultar el historial"
      },
      automationInstruction: {
        title: "Instrucción de automatización",
        show: "Ver instrucción completa",
        hide: "Ocultar instrucción",
        contentLabel: "Contenido de la instrucción de automatización"
      },
      emailReferences: {
        listLabel: "Correos relacionados",
        providers: { gmail: "Gmail", outlook: "Outlook", imap: "Correo IMAP" },
        previewAria: "Ver correo: {{subject}}",
        openAria: "Abrir en {{provider}}: {{subject}}",
        searchAria: "Buscar en {{provider}}: {{subject}}",
        openIn: "Abrir en {{provider}}",
        searchIn: "Buscar en {{provider}}",
        close: "Cerrar vista previa del correo",
        loading: "Cargando correo…",
        loadError: "No se pudo cargar este correo. Inténtalo de nuevo.",
        emptyBody: "Este correo no tiene contenido de texto disponible.",
        citedNotVerified: "Citado por {{agent}} · no verificado con el buzón",
        safePreview: "Vista en texto plano; no carga imágenes ni contenido remoto."
      },
      promptUnavailable: "Este perfil no anunció envío de mensajes.",
      profileReadOnly: "Este perfil está protegido en modo solo lectura.",
      voiceNote: "Nota de voz",
      voiceNoteNumber: "Nota de voz {{number}}",
      playVoiceNote: "Reproducir nota de voz",
      audioUnsupported: "Tu navegador no puede reproducir esta nota de voz.",
      chooseTestEnvironment: "Para escribir, selecciona el entorno de pruebas."
    },
    approvals: {
      choices: { once: "Permitir una vez", session: "Durante esta sesión", always: "Permitir siempre", deny: "Rechazar" },
      actionPaused: "Acción detenida",
      required: "Aprobación requerida",
      unconfirmed: "Sin confirmar",
      waiting: "Esperando",
      smartDenied: "La revisión de seguridad bloqueó esta acción. Solo puedes permitirla una vez o rechazarla.",
      options: "Opciones de aprobación",
      confirming: "Confirmando…",
      reconnect: "Recupera la conexión para responder de forma segura.",
      unavailable: "La respuesta requiere un perfil habilitado por el backend y la capacidad approval.respond verificada.",
      waitingAria: "Hermes espera tu respuesta",
      errors: {
        deliveryUnknown: "El resultado no está confirmado. Los controles permanecerán bloqueados hasta que Hermes reconcilie la solicitud.",
        noLongerPending: "Hermes no confirmó que la solicitud siga pendiente.",
        generic: "No se pudo confirmar la respuesta. Revisa la conexión e inténtalo de nuevo."
      }
    },
    clarifications: {
      omitted: "Omitida",
      answered: "Respondida",
      selectMultiple: "Selecciona una o más opciones",
      selectOne: "Selecciona una opción",
      otherAnswer: "Otra respuesta",
      answerAria: "Respuesta: {{question}}",
      otherAnswerAria: "Otra respuesta: {{question}}",
      writeAnswer: "Escribe una respuesta",
      addOtherOption: "Añade otra opción",
      writeOtherAnswer: "Escribe otra respuesta",
      sending: "Enviando…",
      confirmAnswer: "Confirmar respuesta",
      respond: "Responder",
      needsContext: "Hermes necesita contexto",
      questions_one: "{{count}} pregunta",
      questions_other: "{{count}} preguntas",
      oneQuestion: "Una pregunta antes de continuar",
      unconfirmed: "Sin confirmar",
      waiting: "Esperando",
      reconnect: "Recupera la conexión para responder.",
      unavailable: "La respuesta requiere un perfil habilitado por el backend y la capacidad clarify.respond verificada.",
      errors: {
        deliveryUnknown: "El resultado no está confirmado. Los controles permanecerán bloqueados hasta que Hermes reconcilie la solicitud.",
        noLongerPending: "Hermes no confirmó que la solicitud siga pendiente.",
        generic: "No se pudo confirmar la respuesta. Revisa la conexión e inténtalo de nuevo."
      }
    },
    activity: {
      close: "Cerrar actividad",
      panelAria: "Actividad y contexto",
      activeContext: "Contexto activo",
      sessionDetails: "Detalles de sesión",
      noAgent: "Sin agente",
      undetected: "sin detectar",
      ready: "Listo",
      unavailable: "No disponible",
      workspace: "Workspace",
      noWorkspace: "Sin workspace",
      gateway: "Gateway",
      noGateway: "Sin gateway",
      session: "Sesión",
      noSession: "sin sesión",
      usage: {
        title: "Uso de contexto",
        demoAria: "Uso de contexto · demo",
        tokensUsedDemo: "tokens usados · demo",
        contextWindowDemo: "ventana · demo",
        percentValue: "{{value}} por ciento",
        countersWithoutOccupancy: "Hermes reportó contadores, pero no la ocupación actual del contexto.",
        totalTokens: "tokens acumulados",
        inputTokens: "tokens de entrada",
        outputTokens: "tokens de salida",
        currentContext: "contexto actual",
        contextWindow: "ventana de contexto",
        calls: "llamadas",
        noTelemetry: "Hermes no anunció telemetría de uso para esta sesión."
      },
      tools: "Herramientas",
      noActivity: "Sin actividad",
      toolsWhenReported: "Las herramientas aparecerán cuando Hermes las reporte.",
      subagents: "Subagentes",
      researcher: "Investigador",
      workFinishedDemo: "Trabajo finalizado · demo",
      recentActivity: "Actividad reciente",
      transport: "Transporte {{status}}",
      connection: { connected: "conectado", reconnecting: "reconectando", degraded: "degradado", offline: "sin conexión" },
      stateObserved: "Estado observado por Agent Control",
      runtimeLinked: "Runtime enlazado",
      runtimePending: "Runtime pendiente",
      identityConfirmed: "Identidad confirmada",
      resumesBeforeCommand: "Se reanudará antes del próximo comando",
      sessionActions: "Acciones de sesión",
      exportConversation: "Exportar conversación",
      exportDescription: "Descarga una copia saneada del historial que conserva Hermes.",
      exporting: "Exportando…",
      export: "Exportar",
      archiveInControl: "Archivar en Control",
      archiveDescription: "Oculta esta referencia local. La conversación permanece intacta en Hermes.",
      archiving: "Archivando…",
      archive: "Archivar",
      deleteFromHermes: "Eliminar de Hermes",
      deleteDescription: "Borra permanentemente la conversación de la infraestructura Hermes existente.",
      deleteEllipsis: "Eliminar…",
      actionsRequireOnline: "Estas acciones requieren una sesión online de Agent Control.",
      archivedAnnouncement: "“{{title}}” se archivó solo en Agent Control. La conversación sigue en Hermes.",
      exportedAnnouncement: "Se exportó “{{title}}” desde el historial autoritativo de Hermes.",
      deletedAnnouncement: "“{{title}}” se eliminó de Hermes y de Agent Control.",
      archiveError: "No se pudo archivar la sesión.",
      exportError: "No se pudo exportar la sesión.",
      deleteError: "No se pudo eliminar la sesión de Hermes.",
      advancedMode: "Modo avanzado",
      technicalIds: "IDs técnicos y diagnósticos",
      unassigned: "sin asignar",
      cancelDeleteAria: "Cancelar eliminación de sesión",
      irreversible: "Acción irreversible en Hermes",
      deleteTitle: "Eliminar “{{title}}”",
      deleteDialogDescription: "La conversación y su historial se eliminarán de Hermes y de Agent Control. Esta acción no se puede deshacer.",
      cancel: "Cancelar",
      deleting: "Eliminando…"
    }
  },
  en: {
    chat: {
      conversation: "Conversation", newConversation: "New conversation", fixedDate: "August 28, 2026", toolsCount: "Tools · {{count}}",
      toolStatus: { completed: "Done", failed: "Error", running: "Running" }, delivery: { unconfirmed: "Delivery unconfirmed", delivered: "Delivered", sending: "Sending" },
      userMessage: "Your message", deliveryWarning: "Delivery was not confirmed; it will not be resent automatically.", assistantResponse: "Response from {{agent}}", streaming: "Running",
      messagePlaceholder: "Message {{agent}}…", offlineDraft: "Offline draft", stop: "Stop", running: "Running", sendMessage: "Send message", attachments: { add: "Add to chat", menu: "Add options", image: "Image", imageHint: "JPG, PNG, WebP, or GIF", file: "File", fileHint: "PDF, documents, sheets, text, or code", selected: "Selected files", attached: "Attachments", remove: "Remove {{name}}", errors: { tooMany: "You can attach up to 5 files per message.", tooLarge: "Each file must be no larger than 8 MB.", tooMuchTotal: "Attachments must be no larger than 12 MB in total.", unsupported: "That image type is not supported." } },
      offlineDraftNote: "The draft stays on this device and will not be sent when the connection returns.", disclaimer: "Hermes can make mistakes. Verify important information.",
      agent: "Agent", yourAgent: "your agent", thisAgent: "This agent", startWithAgent: "Start a conversation with {{agent}}", createWithAgent: "Create a chat with {{agent}}", readOnlyAgent: "{{agent}} is in read-only mode",
      sessionIsolation: "This session's context will remain isolated from the other agents.", startInWorkspace: "Start a conversation in this workspace.", readOnlyDescription: "Current protection does not allow creating conversations or sending messages. Select the test environment to write.",
      creating: "Creating…", newChat: "New chat", waitingForResponse: "{{agent}} is waiting for your response", typing: "{{agent}} is typing", activity: { show: "Show {{agent}} activity", hide: "Hide {{agent}} activity", label: "{{agent}} activity", analyzing: "Analyzing the request", composing: "Writing the response", tool: "Tool", delegation: "Delegated work" }, toolEvidence: { show: "Show {{agent}} tool history", hide: "Hide {{agent}} tool history", label: "{{agent}} tool history", showHint: "Select to view history", hideHint: "Select to hide history" }, automationInstruction: { title: "Automation instruction", show: "View full instruction", hide: "Hide instruction", contentLabel: "Automation instruction content" }, emailReferences: { listLabel: "Related emails", providers: { gmail: "Gmail", outlook: "Outlook", imap: "IMAP mail" }, previewAria: "View email: {{subject}}", openAria: "Open in {{provider}}: {{subject}}", searchAria: "Search in {{provider}}: {{subject}}", openIn: "Open in {{provider}}", searchIn: "Search in {{provider}}", close: "Close email preview", loading: "Loading email…", loadError: "This email could not be loaded. Try again.", emptyBody: "This email has no text content available.", citedNotVerified: "Cited by {{agent}} · not mailbox-verified", safePreview: "Plain-text preview; it does not load images or remote content." }, promptUnavailable: "This profile did not advertise message sending.", profileReadOnly: "This profile is protected in read-only mode.", voiceNote: "Voice note", voiceNoteNumber: "Voice note {{number}}", playVoiceNote: "Play voice note", audioUnsupported: "Your browser cannot play this voice note.", chooseTestEnvironment: "To write, select the test environment."
    },
    approvals: {
      choices: { once: "Allow once", session: "For this session", always: "Always allow", deny: "Deny" }, actionPaused: "Action paused", required: "Approval required", unconfirmed: "Unconfirmed", waiting: "Waiting",
      smartDenied: "The security review blocked this action. You can only allow it once or deny it.", options: "Approval options", confirming: "Confirming…", reconnect: "Restore the connection to respond safely.", unavailable: "Responding requires a backend-enabled profile and the verified approval.respond capability.", waitingAria: "Hermes is waiting for your response",
      errors: { deliveryUnknown: "The result is unconfirmed. The controls will remain locked until Hermes reconciles the request.", noLongerPending: "Hermes did not confirm that the request is still pending.", generic: "The response could not be confirmed. Check the connection and try again." }
    },
    clarifications: {
      omitted: "Skipped", answered: "Answered", selectMultiple: "Select one or more options", selectOne: "Select an option", otherAnswer: "Other answer", answerAria: "Answer: {{question}}", otherAnswerAria: "Other answer: {{question}}", writeAnswer: "Write an answer", addOtherOption: "Add another option", writeOtherAnswer: "Write another answer", sending: "Sending…", confirmAnswer: "Confirm answer", respond: "Respond", needsContext: "Hermes needs context", questions_one: "{{count}} question", questions_other: "{{count}} questions", oneQuestion: "One question before continuing", unconfirmed: "Unconfirmed", waiting: "Waiting", reconnect: "Restore the connection to respond.", unavailable: "Responding requires a backend-enabled profile and the verified clarify.respond capability.",
      errors: { deliveryUnknown: "The result is unconfirmed. The controls will remain locked until Hermes reconciles the request.", noLongerPending: "Hermes did not confirm that the request is still pending.", generic: "The response could not be confirmed. Check the connection and try again." }
    },
    activity: {
      close: "Close activity", panelAria: "Activity and context", activeContext: "Active context", sessionDetails: "Session details", noAgent: "No agent", undetected: "not detected", ready: "Ready", unavailable: "Unavailable", workspace: "Workspace", noWorkspace: "No workspace", gateway: "Gateway", noGateway: "No gateway", session: "Session", noSession: "no session",
      usage: { title: "Context usage", demoAria: "Context usage · demo", tokensUsedDemo: "tokens used · demo", contextWindowDemo: "window · demo", percentValue: "{{value}} percent", countersWithoutOccupancy: "Hermes reported counters, but not the current context occupancy.", totalTokens: "total tokens", inputTokens: "input tokens", outputTokens: "output tokens", currentContext: "current context", contextWindow: "context window", calls: "calls", noTelemetry: "Hermes did not advertise usage telemetry for this session." },
      tools: "Tools", noActivity: "No activity", toolsWhenReported: "Tools will appear when Hermes reports them.", subagents: "Subagents", researcher: "Researcher", workFinishedDemo: "Work completed · demo", recentActivity: "Recent activity", transport: "Transport {{status}}", connection: { connected: "connected", reconnecting: "reconnecting", degraded: "degraded", offline: "offline" }, stateObserved: "State observed by Agent Control", runtimeLinked: "Runtime linked", runtimePending: "Runtime pending", identityConfirmed: "Identity confirmed", resumesBeforeCommand: "It will resume before the next command",
      sessionActions: "Session actions", exportConversation: "Export conversation", exportDescription: "Download a sanitized copy of the history retained by Hermes.", exporting: "Exporting…", export: "Export", archiveInControl: "Archive in Control", archiveDescription: "Hide this local reference. The conversation remains intact in Hermes.", archiving: "Archiving…", archive: "Archive", deleteFromHermes: "Delete from Hermes", deleteDescription: "Permanently delete the conversation from the existing Hermes infrastructure.", deleteEllipsis: "Delete…", actionsRequireOnline: "These actions require an online Agent Control session.", archivedAnnouncement: "“{{title}}” was archived only in Agent Control. The conversation remains in Hermes.", exportedAnnouncement: "“{{title}}” was exported from Hermes' authoritative history.", deletedAnnouncement: "“{{title}}” was deleted from Hermes and Agent Control.", archiveError: "The session could not be archived.", exportError: "The session could not be exported.", deleteError: "The session could not be deleted from Hermes.", advancedMode: "Advanced mode", technicalIds: "Technical IDs and diagnostics", unassigned: "unassigned", cancelDeleteAria: "Cancel session deletion", irreversible: "Irreversible action in Hermes", deleteTitle: "Delete “{{title}}”", deleteDialogDescription: "The conversation and its history will be deleted from Hermes and Agent Control. This action cannot be undone.", cancel: "Cancel", deleting: "Deleting…"
    }
  },
  fr: {
    chat: {
      attachments: { add: "Ajouter au chat", menu: "Options d’ajout", image: "Image", imageHint: "JPG, PNG, WebP ou GIF", file: "Fichier", fileHint: "PDF, documents, feuilles, texte ou code", selected: "Fichiers sélectionnés", attached: "Pièces jointes", remove: "Retirer {{name}}", errors: { tooMany: "Vous pouvez joindre jusqu’à 5 fichiers par message.", tooLarge: "Chaque fichier doit faire au maximum 8 Mo.", tooMuchTotal: "Les pièces jointes doivent faire au maximum 12 Mo au total.", unsupported: "Ce type d’image n’est pas pris en charge." } },
      conversation: "Conversation", newConversation: "Nouvelle conversation", fixedDate: "28 août 2026", toolsCount: "Outils · {{count}}", toolStatus: { completed: "Terminé", failed: "Erreur", running: "En cours" }, delivery: { unconfirmed: "Livraison non confirmée", delivered: "Livré", sending: "Envoi" }, userMessage: "Votre message", deliveryWarning: "La livraison n’a pas été confirmée ; le message ne sera pas renvoyé automatiquement.", assistantResponse: "Réponse de {{agent}}", streaming: "En cours", messagePlaceholder: "Message à {{agent}}…", offlineDraft: "Brouillon hors ligne", stop: "Arrêter", running: "En cours", sendMessage: "Envoyer le message", offlineDraftNote: "Le brouillon reste sur cet appareil et ne sera pas envoyé au retour de la connexion.", disclaimer: "Hermes peut commettre des erreurs. Vérifiez les informations importantes.", agent: "Agent", yourAgent: "votre agent", thisAgent: "Cet agent", startWithAgent: "Démarrer une conversation avec {{agent}}", createWithAgent: "Créer une discussion avec {{agent}}", readOnlyAgent: "{{agent}} est en lecture seule", sessionIsolation: "Le contexte de cette session restera isolé des autres agents.", startInWorkspace: "Démarrez une conversation dans cet espace de travail.", readOnlyDescription: "La protection actuelle ne permet ni de créer des conversations ni d’envoyer des messages. Sélectionnez l’environnement de test pour écrire.", creating: "Création…", newChat: "Nouvelle discussion", waitingForResponse: "{{agent}} attend votre réponse", typing: "{{agent}} écrit", activity: { show: "Afficher l’activité de {{agent}}", hide: "Masquer l’activité de {{agent}}", label: "Activité de {{agent}}", analyzing: "Analyse de la demande", composing: "Rédaction de la réponse", tool: "Outil", delegation: "Travail délégué" }, toolEvidence: { show: "Afficher l’historique des outils de {{agent}}", hide: "Masquer l’historique des outils de {{agent}}", label: "Historique des outils de {{agent}}", showHint: "Appuyez pour voir l’historique", hideHint: "Appuyez pour masquer l’historique" }, automationInstruction: { title: "Instruction d’automatisation", show: "Voir l’instruction complète", hide: "Masquer l’instruction", contentLabel: "Contenu de l’instruction d’automatisation" }, emailReferences: { listLabel: "E-mails associés", providers: { gmail: "Gmail", outlook: "Outlook", imap: "Courrier IMAP" }, previewAria: "Voir l’e-mail : {{subject}}", openAria: "Ouvrir dans {{provider}} : {{subject}}", searchAria: "Rechercher dans {{provider}} : {{subject}}", openIn: "Ouvrir dans {{provider}}", searchIn: "Rechercher dans {{provider}}", close: "Fermer l’aperçu de l’e-mail", loading: "Chargement de l’e-mail…", loadError: "Impossible de charger cet e-mail. Réessayez.", emptyBody: "Aucun contenu texte n’est disponible pour cet e-mail.", citedNotVerified: "Cité par {{agent}} · non vérifié dans la boîte mail", safePreview: "Aperçu en texte brut ; aucune image ni contenu distant n’est chargé." }, promptUnavailable: "Ce profil n’a pas annoncé l’envoi de messages.", profileReadOnly: "Ce profil est protégé en lecture seule.", voiceNote: "Note vocale", voiceNoteNumber: "Note vocale {{number}}", playVoiceNote: "Lire la note vocale", audioUnsupported: "Votre navigateur ne peut pas lire cette note vocale.", chooseTestEnvironment: "Pour écrire, sélectionnez l’environnement de test."
    },
    approvals: { choices: { once: "Autoriser une fois", session: "Pour cette session", always: "Toujours autoriser", deny: "Refuser" }, actionPaused: "Action suspendue", required: "Approbation requise", unconfirmed: "Non confirmé", waiting: "En attente", smartDenied: "L’examen de sécurité a bloqué cette action. Vous pouvez uniquement l’autoriser une fois ou la refuser.", options: "Options d’approbation", confirming: "Confirmation…", reconnect: "Rétablissez la connexion pour répondre en toute sécurité.", unavailable: "La réponse nécessite un profil activé par le backend et la capacité approval.respond vérifiée.", waitingAria: "Hermes attend votre réponse", errors: { deliveryUnknown: "Le résultat n’est pas confirmé. Les commandes resteront verrouillées jusqu’à ce que Hermes réconcilie la demande.", noLongerPending: "Hermes n’a pas confirmé que la demande est toujours en attente.", generic: "La réponse n’a pas pu être confirmée. Vérifiez la connexion et réessayez." } },
    clarifications: { omitted: "Ignorée", answered: "Répondue", selectMultiple: "Sélectionnez une ou plusieurs options", selectOne: "Sélectionnez une option", otherAnswer: "Autre réponse", answerAria: "Réponse : {{question}}", otherAnswerAria: "Autre réponse : {{question}}", writeAnswer: "Écrivez une réponse", addOtherOption: "Ajoutez une autre option", writeOtherAnswer: "Écrivez une autre réponse", sending: "Envoi…", confirmAnswer: "Confirmer la réponse", respond: "Répondre", needsContext: "Hermes a besoin de contexte", questions_one: "{{count}} question", questions_other: "{{count}} questions", oneQuestion: "Une question avant de continuer", unconfirmed: "Non confirmé", waiting: "En attente", reconnect: "Rétablissez la connexion pour répondre.", unavailable: "La réponse nécessite un profil activé par le backend et la capacité clarify.respond vérifiée.", errors: { deliveryUnknown: "Le résultat n’est pas confirmé. Les commandes resteront verrouillées jusqu’à ce que Hermes réconcilie la demande.", noLongerPending: "Hermes n’a pas confirmé que la demande est toujours en attente.", generic: "La réponse n’a pas pu être confirmée. Vérifiez la connexion et réessayez." } },
    activity: {
      close: "Fermer l’activité", panelAria: "Activité et contexte", activeContext: "Contexte actif", sessionDetails: "Détails de la session", noAgent: "Aucun agent", undetected: "non détecté", ready: "Prêt", unavailable: "Indisponible", workspace: "Espace de travail", noWorkspace: "Aucun espace de travail", gateway: "Passerelle", noGateway: "Aucune passerelle", session: "Session", noSession: "aucune session", usage: { title: "Utilisation du contexte", demoAria: "Utilisation du contexte · démo", tokensUsedDemo: "jetons utilisés · démo", contextWindowDemo: "fenêtre · démo", percentValue: "{{value}} pour cent", countersWithoutOccupancy: "Hermes a signalé des compteurs, mais pas l’occupation actuelle du contexte.", totalTokens: "jetons cumulés", inputTokens: "jetons d’entrée", outputTokens: "jetons de sortie", currentContext: "contexte actuel", contextWindow: "fenêtre de contexte", calls: "appels", noTelemetry: "Hermes n’a pas annoncé de télémétrie d’utilisation pour cette session." }, tools: "Outils", noActivity: "Aucune activité", toolsWhenReported: "Les outils apparaîtront lorsque Hermes les signalera.", subagents: "Sous-agents", researcher: "Chercheur", workFinishedDemo: "Travail terminé · démo", recentActivity: "Activité récente", transport: "Transport {{status}}", connection: { connected: "connecté", reconnecting: "reconnexion", degraded: "dégradé", offline: "hors ligne" }, stateObserved: "État observé par Agent Control", runtimeLinked: "Runtime lié", runtimePending: "Runtime en attente", identityConfirmed: "Identité confirmée", resumesBeforeCommand: "Il reprendra avant la prochaine commande", sessionActions: "Actions de session", exportConversation: "Exporter la conversation", exportDescription: "Téléchargez une copie expurgée de l’historique conservé par Hermes.", exporting: "Exportation…", export: "Exporter", archiveInControl: "Archiver dans Control", archiveDescription: "Masque cette référence locale. La conversation reste intacte dans Hermes.", archiving: "Archivage…", archive: "Archiver", deleteFromHermes: "Supprimer de Hermes", deleteDescription: "Supprime définitivement la conversation de l’infrastructure Hermes existante.", deleteEllipsis: "Supprimer…", actionsRequireOnline: "Ces actions nécessitent une session Agent Control en ligne.", archivedAnnouncement: "« {{title}} » a été archivée uniquement dans Agent Control. La conversation reste dans Hermes.", exportedAnnouncement: "« {{title}} » a été exportée depuis l’historique de référence de Hermes.", deletedAnnouncement: "« {{title}} » a été supprimée de Hermes et d’Agent Control.", archiveError: "La session n’a pas pu être archivée.", exportError: "La session n’a pas pu être exportée.", deleteError: "La session n’a pas pu être supprimée de Hermes.", advancedMode: "Mode avancé", technicalIds: "ID techniques et diagnostics", unassigned: "non attribué", cancelDeleteAria: "Annuler la suppression de la session", irreversible: "Action irréversible dans Hermes", deleteTitle: "Supprimer « {{title}} »", deleteDialogDescription: "La conversation et son historique seront supprimés de Hermes et d’Agent Control. Cette action est irréversible.", cancel: "Annuler", deleting: "Suppression…"
    }
  },
  de: {
    chat: {
      attachments: { add: "Zum Chat hinzufügen", menu: "Optionen zum Hinzufügen", image: "Bild", imageHint: "JPG, PNG, WebP oder GIF", file: "Datei", fileHint: "PDF, Dokumente, Tabellen, Text oder Code", selected: "Ausgewählte Dateien", attached: "Anhänge", remove: "{{name}} entfernen", errors: { tooMany: "Sie können bis zu 5 Dateien pro Nachricht anhängen.", tooLarge: "Jede Datei darf höchstens 8 MB groß sein.", tooMuchTotal: "Anhänge dürfen insgesamt höchstens 12 MB groß sein.", unsupported: "Dieser Bildtyp wird nicht unterstützt." } },
      conversation: "Unterhaltung", newConversation: "Neue Unterhaltung", fixedDate: "28. August 2026", toolsCount: "Werkzeuge · {{count}}", toolStatus: { completed: "Fertig", failed: "Fehler", running: "Läuft" }, delivery: { unconfirmed: "Zustellung unbestätigt", delivered: "Zugestellt", sending: "Wird gesendet" }, userMessage: "Ihre Nachricht", deliveryWarning: "Die Zustellung wurde nicht bestätigt; die Nachricht wird nicht automatisch erneut gesendet.", assistantResponse: "Antwort von {{agent}}", streaming: "Läuft", messagePlaceholder: "Nachricht an {{agent}}…", offlineDraft: "Offline-Entwurf", stop: "Stoppen", running: "Wird ausgeführt", sendMessage: "Nachricht senden", offlineDraftNote: "Der Entwurf bleibt auf diesem Gerät und wird bei erneuter Verbindung nicht gesendet.", disclaimer: "Hermes kann Fehler machen. Prüfen Sie wichtige Informationen.", agent: "Agent", yourAgent: "Ihrem Agenten", thisAgent: "Dieser Agent", startWithAgent: "Unterhaltung mit {{agent}} beginnen", createWithAgent: "Chat mit {{agent}} erstellen", readOnlyAgent: "{{agent}} ist schreibgeschützt", sessionIsolation: "Der Kontext dieser Sitzung bleibt von den anderen Agenten getrennt.", startInWorkspace: "Beginnen Sie eine Unterhaltung in diesem Arbeitsbereich.", readOnlyDescription: "Der aktuelle Schutz erlaubt weder das Erstellen von Unterhaltungen noch das Senden von Nachrichten. Wählen Sie zum Schreiben die Testumgebung.", creating: "Wird erstellt…", newChat: "Neuer Chat", waitingForResponse: "{{agent}} wartet auf Ihre Antwort", typing: "{{agent}} schreibt", activity: { show: "Aktivität von {{agent}} anzeigen", hide: "Aktivität von {{agent}} ausblenden", label: "Aktivität von {{agent}}", analyzing: "Anfrage wird analysiert", composing: "Antwort wird verfasst", tool: "Werkzeug", delegation: "Delegierte Arbeit" }, toolEvidence: { show: "Werkzeugverlauf von {{agent}} anzeigen", hide: "Werkzeugverlauf von {{agent}} ausblenden", label: "Werkzeugverlauf von {{agent}}", showHint: "Auswählen, um den Verlauf anzuzeigen", hideHint: "Auswählen, um den Verlauf auszublenden" }, automationInstruction: { title: "Automatisierungsanweisung", show: "Vollständige Anweisung anzeigen", hide: "Anweisung ausblenden", contentLabel: "Inhalt der Automatisierungsanweisung" }, emailReferences: { listLabel: "Zugehörige E-Mails", providers: { gmail: "Gmail", outlook: "Outlook", imap: "IMAP-E-Mail" }, previewAria: "E-Mail anzeigen: {{subject}}", openAria: "In {{provider}} öffnen: {{subject}}", searchAria: "In {{provider}} suchen: {{subject}}", openIn: "In {{provider}} öffnen", searchIn: "In {{provider}} suchen", close: "E-Mail-Vorschau schließen", loading: "E-Mail wird geladen…", loadError: "Diese E-Mail konnte nicht geladen werden. Versuchen Sie es erneut.", emptyBody: "Für diese E-Mail ist kein Textinhalt verfügbar.", citedNotVerified: "Von {{agent}} zitiert · nicht im Postfach verifiziert", safePreview: "Nur-Text-Vorschau; Bilder und externe Inhalte werden nicht geladen." }, promptUnavailable: "Dieses Profil hat das Senden von Nachrichten nicht angekündigt.", profileReadOnly: "Dieses Profil ist schreibgeschützt.", voiceNote: "Sprachnotiz", voiceNoteNumber: "Sprachnotiz {{number}}", playVoiceNote: "Sprachnotiz abspielen", audioUnsupported: "Ihr Browser kann diese Sprachnotiz nicht wiedergeben.", chooseTestEnvironment: "Wählen Sie zum Schreiben die Testumgebung."
    },
    approvals: { choices: { once: "Einmal erlauben", session: "Für diese Sitzung", always: "Immer erlauben", deny: "Ablehnen" }, actionPaused: "Aktion angehalten", required: "Genehmigung erforderlich", unconfirmed: "Unbestätigt", waiting: "Wartet", smartDenied: "Die Sicherheitsprüfung hat diese Aktion blockiert. Sie können sie nur einmal erlauben oder ablehnen.", options: "Genehmigungsoptionen", confirming: "Wird bestätigt…", reconnect: "Stellen Sie die Verbindung wieder her, um sicher zu antworten.", unavailable: "Zum Antworten sind ein vom Backend freigegebenes Profil und die verifizierte Fähigkeit approval.respond erforderlich.", waitingAria: "Hermes wartet auf Ihre Antwort", errors: { deliveryUnknown: "Das Ergebnis ist unbestätigt. Die Bedienelemente bleiben gesperrt, bis Hermes die Anfrage abgeglichen hat.", noLongerPending: "Hermes hat nicht bestätigt, dass die Anfrage noch aussteht.", generic: "Die Antwort konnte nicht bestätigt werden. Prüfen Sie die Verbindung und versuchen Sie es erneut." } },
    clarifications: { omitted: "Übersprungen", answered: "Beantwortet", selectMultiple: "Wählen Sie eine oder mehrere Optionen", selectOne: "Wählen Sie eine Option", otherAnswer: "Andere Antwort", answerAria: "Antwort: {{question}}", otherAnswerAria: "Andere Antwort: {{question}}", writeAnswer: "Antwort eingeben", addOtherOption: "Weitere Option hinzufügen", writeOtherAnswer: "Andere Antwort eingeben", sending: "Wird gesendet…", confirmAnswer: "Antwort bestätigen", respond: "Antworten", needsContext: "Hermes benötigt Kontext", questions_one: "{{count}} Frage", questions_other: "{{count}} Fragen", oneQuestion: "Eine Frage vor dem Fortfahren", unconfirmed: "Unbestätigt", waiting: "Wartet", reconnect: "Stellen Sie die Verbindung wieder her, um zu antworten.", unavailable: "Zum Antworten sind ein vom Backend freigegebenes Profil und die verifizierte Fähigkeit clarify.respond erforderlich.", errors: { deliveryUnknown: "Das Ergebnis ist unbestätigt. Die Bedienelemente bleiben gesperrt, bis Hermes die Anfrage abgeglichen hat.", noLongerPending: "Hermes hat nicht bestätigt, dass die Anfrage noch aussteht.", generic: "Die Antwort konnte nicht bestätigt werden. Prüfen Sie die Verbindung und versuchen Sie es erneut." } },
    activity: {
      close: "Aktivität schließen", panelAria: "Aktivität und Kontext", activeContext: "Aktiver Kontext", sessionDetails: "Sitzungsdetails", noAgent: "Kein Agent", undetected: "nicht erkannt", ready: "Bereit", unavailable: "Nicht verfügbar", workspace: "Arbeitsbereich", noWorkspace: "Kein Arbeitsbereich", gateway: "Gateway", noGateway: "Kein Gateway", session: "Sitzung", noSession: "keine Sitzung", usage: { title: "Kontextnutzung", demoAria: "Kontextnutzung · Demo", tokensUsedDemo: "verwendete Token · Demo", contextWindowDemo: "Fenster · Demo", percentValue: "{{value}} Prozent", countersWithoutOccupancy: "Hermes hat Zähler gemeldet, aber nicht die aktuelle Kontextbelegung.", totalTokens: "Token insgesamt", inputTokens: "Eingabe-Token", outputTokens: "Ausgabe-Token", currentContext: "aktueller Kontext", contextWindow: "Kontextfenster", calls: "Aufrufe", noTelemetry: "Hermes hat für diese Sitzung keine Nutzungstelemetrie angekündigt." }, tools: "Werkzeuge", noActivity: "Keine Aktivität", toolsWhenReported: "Werkzeuge erscheinen, sobald Hermes sie meldet.", subagents: "Unteragenten", researcher: "Rechercheur", workFinishedDemo: "Arbeit abgeschlossen · Demo", recentActivity: "Letzte Aktivität", transport: "Transport {{status}}", connection: { connected: "verbunden", reconnecting: "wird wieder verbunden", degraded: "beeinträchtigt", offline: "offline" }, stateObserved: "Von Agent Control beobachteter Status", runtimeLinked: "Runtime verknüpft", runtimePending: "Runtime ausstehend", identityConfirmed: "Identität bestätigt", resumesBeforeCommand: "Wird vor dem nächsten Befehl fortgesetzt", sessionActions: "Sitzungsaktionen", exportConversation: "Unterhaltung exportieren", exportDescription: "Laden Sie eine bereinigte Kopie des von Hermes gespeicherten Verlaufs herunter.", exporting: "Wird exportiert…", export: "Exportieren", archiveInControl: "In Control archivieren", archiveDescription: "Blendet diese lokale Referenz aus. Die Unterhaltung bleibt in Hermes unverändert.", archiving: "Wird archiviert…", archive: "Archivieren", deleteFromHermes: "Aus Hermes löschen", deleteDescription: "Löscht die Unterhaltung dauerhaft aus der vorhandenen Hermes-Infrastruktur.", deleteEllipsis: "Löschen…", actionsRequireOnline: "Diese Aktionen erfordern eine aktive Agent-Control-Sitzung.", archivedAnnouncement: "„{{title}}“ wurde nur in Agent Control archiviert. Die Unterhaltung bleibt in Hermes.", exportedAnnouncement: "„{{title}}“ wurde aus dem maßgeblichen Hermes-Verlauf exportiert.", deletedAnnouncement: "„{{title}}“ wurde aus Hermes und Agent Control gelöscht.", archiveError: "Die Sitzung konnte nicht archiviert werden.", exportError: "Die Sitzung konnte nicht exportiert werden.", deleteError: "Die Sitzung konnte nicht aus Hermes gelöscht werden.", advancedMode: "Erweiterter Modus", technicalIds: "Technische IDs und Diagnose", unassigned: "nicht zugewiesen", cancelDeleteAria: "Löschen der Sitzung abbrechen", irreversible: "Unumkehrbare Aktion in Hermes", deleteTitle: "„{{title}}“ löschen", deleteDialogDescription: "Die Unterhaltung und ihr Verlauf werden aus Hermes und Agent Control gelöscht. Diese Aktion kann nicht rückgängig gemacht werden.", cancel: "Abbrechen", deleting: "Wird gelöscht…"
    }
  },
  pt: {
    chat: {
      attachments: { add: "Adicionar ao chat", menu: "Opções para adicionar", image: "Imagem", imageHint: "JPG, PNG, WebP ou GIF", file: "Arquivo", fileHint: "PDF, documentos, planilhas, texto ou código", selected: "Arquivos selecionados", attached: "Anexos", remove: "Remover {{name}}", errors: { tooMany: "Você pode anexar até 5 arquivos por mensagem.", tooLarge: "Cada arquivo deve ter no máximo 8 MB.", tooMuchTotal: "Os anexos devem ter no máximo 12 MB no total.", unsupported: "Esse tipo de imagem não é compatível." } },
      conversation: "Conversa", newConversation: "Nova conversa", fixedDate: "28 de agosto de 2026", toolsCount: "Ferramentas · {{count}}", toolStatus: { completed: "Concluído", failed: "Erro", running: "Em andamento" }, delivery: { unconfirmed: "Entrega não confirmada", delivered: "Entregue", sending: "Enviando" }, userMessage: "Sua mensagem", deliveryWarning: "A entrega não foi confirmada; a mensagem não será reenviada automaticamente.", assistantResponse: "Resposta de {{agent}}", streaming: "Em andamento", messagePlaceholder: "Mensagem para {{agent}}…", offlineDraft: "Rascunho offline", stop: "Parar", running: "Em execução", sendMessage: "Enviar mensagem", offlineDraftNote: "O rascunho permanece neste dispositivo e não será enviado quando a conexão voltar.", disclaimer: "Hermes pode cometer erros. Verifique informações importantes.", agent: "Agente", yourAgent: "seu agente", thisAgent: "Este agente", startWithAgent: "Inicie uma conversa com {{agent}}", createWithAgent: "Crie um chat com {{agent}}", readOnlyAgent: "{{agent}} está no modo somente leitura", sessionIsolation: "O contexto desta sessão permanecerá isolado dos outros agentes.", startInWorkspace: "Inicie uma conversa neste espaço de trabalho.", readOnlyDescription: "A proteção atual não permite criar conversas nem enviar mensagens. Selecione o ambiente de testes para escrever.", creating: "Criando…", newChat: "Novo chat", waitingForResponse: "{{agent}} aguarda sua resposta", typing: "{{agent}} está digitando", activity: { show: "Mostrar atividade de {{agent}}", hide: "Ocultar atividade de {{agent}}", label: "Atividade de {{agent}}", analyzing: "Analisando a solicitação", composing: "Redigindo a resposta", tool: "Ferramenta", delegation: "Trabalho delegado" }, toolEvidence: { show: "Mostrar histórico de ferramentas de {{agent}}", hide: "Ocultar histórico de ferramentas de {{agent}}", label: "Histórico de ferramentas de {{agent}}", showHint: "Toque para ver o histórico", hideHint: "Toque para ocultar histórico" }, automationInstruction: { title: "Instrução de automação", show: "Ver instrução completa", hide: "Ocultar instrução", contentLabel: "Conteúdo da instrução de automação" }, emailReferences: { listLabel: "E-mails relacionados", providers: { gmail: "Gmail", outlook: "Outlook", imap: "E-mail IMAP" }, previewAria: "Ver e-mail: {{subject}}", openAria: "Abrir no {{provider}}: {{subject}}", searchAria: "Pesquisar no {{provider}}: {{subject}}", openIn: "Abrir no {{provider}}", searchIn: "Pesquisar no {{provider}}", close: "Fechar visualização do e-mail", loading: "Carregando e-mail…", loadError: "Não foi possível carregar este e-mail. Tente novamente.", emptyBody: "Este e-mail não tem conteúdo de texto disponível.", citedNotVerified: "Citado por {{agent}} · não verificado na caixa de correio", safePreview: "Visualização em texto simples; não carrega imagens nem conteúdo remoto." }, promptUnavailable: "Este perfil não anunciou o envio de mensagens.", profileReadOnly: "Este perfil está protegido no modo somente leitura.", voiceNote: "Nota de voz", voiceNoteNumber: "Nota de voz {{number}}", playVoiceNote: "Reproduzir nota de voz", audioUnsupported: "Seu navegador não pode reproduzir esta nota de voz.", chooseTestEnvironment: "Para escrever, selecione o ambiente de testes."
    },
    approvals: { choices: { once: "Permitir uma vez", session: "Durante esta sessão", always: "Permitir sempre", deny: "Recusar" }, actionPaused: "Ação interrompida", required: "Aprovação necessária", unconfirmed: "Não confirmado", waiting: "Aguardando", smartDenied: "A revisão de segurança bloqueou esta ação. Você só pode permiti-la uma vez ou recusá-la.", options: "Opções de aprovação", confirming: "Confirmando…", reconnect: "Recupere a conexão para responder com segurança.", unavailable: "A resposta requer um perfil habilitado pelo backend e a capacidade approval.respond verificada.", waitingAria: "Hermes aguarda sua resposta", errors: { deliveryUnknown: "O resultado não foi confirmado. Os controles permanecerão bloqueados até que o Hermes reconcilie a solicitação.", noLongerPending: "O Hermes não confirmou que a solicitação ainda está pendente.", generic: "Não foi possível confirmar a resposta. Verifique a conexão e tente novamente." } },
    clarifications: { omitted: "Ignorada", answered: "Respondida", selectMultiple: "Selecione uma ou mais opções", selectOne: "Selecione uma opção", otherAnswer: "Outra resposta", answerAria: "Resposta: {{question}}", otherAnswerAria: "Outra resposta: {{question}}", writeAnswer: "Escreva uma resposta", addOtherOption: "Adicione outra opção", writeOtherAnswer: "Escreva outra resposta", sending: "Enviando…", confirmAnswer: "Confirmar resposta", respond: "Responder", needsContext: "Hermes precisa de contexto", questions_one: "{{count}} pergunta", questions_other: "{{count}} perguntas", oneQuestion: "Uma pergunta antes de continuar", unconfirmed: "Não confirmado", waiting: "Aguardando", reconnect: "Recupere a conexão para responder.", unavailable: "A resposta requer um perfil habilitado pelo backend e a capacidade clarify.respond verificada.", errors: { deliveryUnknown: "O resultado não foi confirmado. Os controles permanecerão bloqueados até que o Hermes reconcilie a solicitação.", noLongerPending: "O Hermes não confirmou que a solicitação ainda está pendente.", generic: "Não foi possível confirmar a resposta. Verifique a conexão e tente novamente." } },
    activity: {
      close: "Fechar atividade", panelAria: "Atividade e contexto", activeContext: "Contexto ativo", sessionDetails: "Detalhes da sessão", noAgent: "Sem agente", undetected: "não detectado", ready: "Pronto", unavailable: "Indisponível", workspace: "Espaço de trabalho", noWorkspace: "Sem espaço de trabalho", gateway: "Gateway", noGateway: "Sem gateway", session: "Sessão", noSession: "sem sessão", usage: { title: "Uso do contexto", demoAria: "Uso do contexto · demo", tokensUsedDemo: "tokens usados · demo", contextWindowDemo: "janela · demo", percentValue: "{{value}} por cento", countersWithoutOccupancy: "Hermes informou contadores, mas não a ocupação atual do contexto.", totalTokens: "tokens acumulados", inputTokens: "tokens de entrada", outputTokens: "tokens de saída", currentContext: "contexto atual", contextWindow: "janela de contexto", calls: "chamadas", noTelemetry: "Hermes não anunciou telemetria de uso para esta sessão." }, tools: "Ferramentas", noActivity: "Sem atividade", toolsWhenReported: "As ferramentas aparecerão quando Hermes as informar.", subagents: "Subagentes", researcher: "Pesquisador", workFinishedDemo: "Trabalho concluído · demo", recentActivity: "Atividade recente", transport: "Transporte {{status}}", connection: { connected: "conectado", reconnecting: "reconectando", degraded: "degradado", offline: "offline" }, stateObserved: "Estado observado pelo Agent Control", runtimeLinked: "Runtime vinculado", runtimePending: "Runtime pendente", identityConfirmed: "Identidade confirmada", resumesBeforeCommand: "Será retomado antes do próximo comando", sessionActions: "Ações da sessão", exportConversation: "Exportar conversa", exportDescription: "Baixe uma cópia saneada do histórico mantido pelo Hermes.", exporting: "Exportando…", export: "Exportar", archiveInControl: "Arquivar no Control", archiveDescription: "Oculta esta referência local. A conversa permanece intacta no Hermes.", archiving: "Arquivando…", archive: "Arquivar", deleteFromHermes: "Excluir do Hermes", deleteDescription: "Exclui permanentemente a conversa da infraestrutura Hermes existente.", deleteEllipsis: "Excluir…", actionsRequireOnline: "Estas ações exigem uma sessão online do Agent Control.", archivedAnnouncement: "“{{title}}” foi arquivada apenas no Agent Control. A conversa continua no Hermes.", exportedAnnouncement: "“{{title}}” foi exportada do histórico oficial do Hermes.", deletedAnnouncement: "“{{title}}” foi excluída do Hermes e do Agent Control.", archiveError: "Não foi possível arquivar a sessão.", exportError: "Não foi possível exportar a sessão.", deleteError: "Não foi possível excluir a sessão do Hermes.", advancedMode: "Modo avançado", technicalIds: "IDs técnicos e diagnósticos", unassigned: "não atribuído", cancelDeleteAria: "Cancelar exclusão da sessão", irreversible: "Ação irreversível no Hermes", deleteTitle: "Excluir “{{title}}”", deleteDialogDescription: "A conversa e seu histórico serão excluídos do Hermes e do Agent Control. Esta ação não pode ser desfeita.", cancel: "Cancelar", deleting: "Excluindo…"
    }
  }
} as const;

export type ChatLocale = keyof typeof chatResources;
