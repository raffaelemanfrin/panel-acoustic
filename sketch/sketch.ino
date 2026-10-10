const float costanteSol = 2.0;

// Soglie per la classificazione dell'urto
const int SOGLIA_MINIMA = 10;   // Ignora il rumore di fondo (< 10)
const int SOGLIA_MEDIO  = 50;  // Da 100 in su è urto medio
const int SOGLIA_FORTE  = 400;  // Da 400 in su è grande urto

void setup() {
  analogReference(AR_INTERNAL);
  pinMode(A0, INPUT);
  Serial.begin(115200); // Velocità aumentata a 115200 per campionare più rapidamente
}

void loop() {

  analogRead(A0);
  int valorePiezo = analogRead(A0);

  // Stampa solo se il piezo supera la soglia minima
  if (valorePiezo >= SOGLIA_MINIMA) {
    if (valorePiezo >= SOGLIA_FORTE) {
      Serial.print("GRANDE URTO (Valore: ");
      Serial.print(valorePiezo);
      Serial.println(")");
    } 
    else if (valorePiezo >= SOGLIA_MEDIO) {
      Serial.print("URTO MEDIO (Valore: ");
      Serial.print(valorePiezo);
      Serial.println(")");
    } 
    else {
      Serial.print("PICCOLO URTO (Valore: ");
      Serial.print(valorePiezo);
      Serial.println(")");
    }

    // Breve pausa per evitare che le oscillazioni di rimbalzo
    // dello stesso colpo generino stampe multiple ripetute
    delay(50);
  }
}