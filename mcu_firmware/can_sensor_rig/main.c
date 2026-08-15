/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : CAN Sensor Rig — STM32F103C8T6 (Blue Pill)
  *
  * Reads HC-SR04 (distance), DHT11 (temp+humidity), LDR (light) and
  * broadcasts one 8-byte CAN frame every 500 ms at 500 kbps.
  *
  * Frame ID 0x123, DLC 8:
  *   Bytes 0-1  distance_mm   uint16 LE  mm
  *   Byte  2    temperature_c int8       degC
  *   Byte  3    humidity_pct  uint8      %
  *   Bytes 4-5  light_raw     uint16 LE  0..4095
  *   Byte  6    light_pct     uint8      %
  *   Byte  7    seq           uint8      rolling counter
  *
  * Pin assignments:
  *   PA0   ADC1_IN0   LDR midpoint (LDR->3V3, 10k->GND)
  *   PA11  CAN_RX     SN65HVD230 CRX
  *   PA12  CAN_TX     SN65HVD230 CTX
  *   PA13  SWDIO      ST-LINK (do not use)
  *   PA14  SWCLK      ST-LINK (do not use)
  *   PB6   DHT11 DATA (10k pull-up to 3V3)
  *   PB8   HC-SR04 TRIG
  *   PB9   HC-SR04 ECHO (via 1k/2k divider — echo is 5V)
  *   PC13  Heartbeat LED (toggles each TX)
  ******************************************************************************
  */
/* USER CODE END Header */

#include "main.h"

/* USER CODE BEGIN PV */
/* Peripheral handles — declared here because CubeMX was not used to
   configure CAN / TIM1 / ADC1. If you later add them in the .ioc and
   regenerate, CubeMX will add its own declarations; remove these three. */
CAN_HandleTypeDef hcan;
TIM_HandleTypeDef htim1;
ADC_HandleTypeDef hadc1;

CAN_TxHeaderTypeDef txHeader;
uint32_t            txMailbox;
uint8_t             txData[8];

#define DHT_PORT  GPIOB
#define DHT_PIN   GPIO_PIN_6
#define TRIG_PORT GPIOB
#define TRIG_PIN  GPIO_PIN_8
#define ECHO_PORT GPIOB
#define ECHO_PIN  GPIO_PIN_9
/* USER CODE END PV */

/* Private function prototypes */
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_CAN_Init(void);
static void MX_TIM1_Init(void);
static void MX_ADC1_Init(void);
void Error_Handler(void);

/* USER CODE BEGIN 0 */

/* --- Microsecond delay (TIM1 running at 1 us/tick) --- */
static void delay_us(uint16_t us)
{
    __HAL_TIM_SET_COUNTER(&htim1, 0);
    while (__HAL_TIM_GET_COUNTER(&htim1) < us);
}

/* --- DHT11 on PB6 (open-drain + external 10k pull-up to 3V3) --- */
static void DHT_SetOutput(void)
{
    GPIO_InitTypeDef g = {0};
    g.Pin   = DHT_PIN;
    g.Mode  = GPIO_MODE_OUTPUT_OD;
    g.Pull  = GPIO_NOPULL;
    g.Speed = GPIO_SPEED_FREQ_HIGH;
    HAL_GPIO_Init(DHT_PORT, &g);
}

static void DHT_SetInput(void)
{
    GPIO_InitTypeDef g = {0};
    g.Pin  = DHT_PIN;
    g.Mode = GPIO_MODE_INPUT;
    g.Pull = GPIO_PULLUP;
    HAL_GPIO_Init(DHT_PORT, &g);
}

/* Returns 1 on success, 0 on timeout or checksum fail */
static uint8_t DHT11_Read(uint8_t *out_temp, uint8_t *out_hum)
{
    uint8_t  bits[5] = {0};
    uint32_t t;

    /* Host start: pull low >= 18 ms then release */
    DHT_SetOutput();
    HAL_GPIO_WritePin(DHT_PORT, DHT_PIN, GPIO_PIN_RESET);
    HAL_Delay(20);
    HAL_GPIO_WritePin(DHT_PORT, DHT_PIN, GPIO_PIN_SET);
    delay_us(30);
    DHT_SetInput();

    /* Sensor response: ~80 us low, ~80 us high */
    t = 0; while ( HAL_GPIO_ReadPin(DHT_PORT, DHT_PIN)) { if (++t > 200) return 0; delay_us(1); }
    t = 0; while (!HAL_GPIO_ReadPin(DHT_PORT, DHT_PIN)) { if (++t > 200) return 0; delay_us(1); }
    t = 0; while ( HAL_GPIO_ReadPin(DHT_PORT, DHT_PIN)) { if (++t > 200) return 0; delay_us(1); }

    /* 40 data bits */
    for (int i = 0; i < 40; i++) {
        t = 0; while (!HAL_GPIO_ReadPin(DHT_PORT, DHT_PIN)) { if (++t > 100) return 0; delay_us(1); }
        delay_us(40);   /* still high after 40 us => '1', already low => '0' */
        if (HAL_GPIO_ReadPin(DHT_PORT, DHT_PIN)) {
            bits[i / 8] |= (1 << (7 - (i % 8)));
            t = 0; while (HAL_GPIO_ReadPin(DHT_PORT, DHT_PIN)) { if (++t > 100) return 0; delay_us(1); }
        }
    }

    if ((uint8_t)(bits[0] + bits[1] + bits[2] + bits[3]) != bits[4]) return 0;
    *out_hum  = bits[0];
    *out_temp = bits[2];
    return 1;
}

/* --- HC-SR04 distance in mm --- */
static uint16_t HCSR04_Read(void)
{
    HAL_GPIO_WritePin(TRIG_PORT, TRIG_PIN, GPIO_PIN_SET);
    delay_us(10);
    HAL_GPIO_WritePin(TRIG_PORT, TRIG_PIN, GPIO_PIN_RESET);

    __HAL_TIM_SET_COUNTER(&htim1, 0);
    while (!HAL_GPIO_ReadPin(ECHO_PORT, ECHO_PIN))
        if (__HAL_TIM_GET_COUNTER(&htim1) > 30000) return 0;   /* no echo timeout */

    __HAL_TIM_SET_COUNTER(&htim1, 0);
    while ( HAL_GPIO_ReadPin(ECHO_PORT, ECHO_PIN))
        if (__HAL_TIM_GET_COUNTER(&htim1) > 30000) return 0;   /* pulse too long */

    uint32_t us = __HAL_TIM_GET_COUNTER(&htim1);
    uint32_t mm = (us * 343UL) / 2000UL;   /* 343 m/s speed of sound, round-trip /2 */
    return (mm > 8000) ? 8000 : (uint16_t)mm;
}

/* --- LDR via ADC1_IN0 --- */
static uint16_t LDR_Read(void)
{
    HAL_ADC_Start(&hadc1);
    HAL_ADC_PollForConversion(&hadc1, 10);
    uint16_t v = HAL_ADC_GetValue(&hadc1);
    HAL_ADC_Stop(&hadc1);
    return v;   /* 0..4095 */
}

/* USER CODE END 0 */


/* ============================================================
   main()
   ============================================================ */
int main(void)
{
    /* USER CODE BEGIN 1 */
    /* USER CODE END 1 */

    HAL_Init();
    SystemClock_Config();

    /* USER CODE BEGIN Init */
    /* USER CODE END Init */

    /* USER CODE BEGIN SysInit */
    /* USER CODE END SysInit */

    MX_GPIO_Init();
    MX_TIM1_Init();
    MX_ADC1_Init();
    MX_CAN_Init();

    /* USER CODE BEGIN 2 */
    HAL_TIM_Base_Start(&htim1);

    /* CAN accept-all filter (we only TX, but HAL requires at least one) */
    CAN_FilterTypeDef sf = {0};
    sf.FilterBank           = 0;
    sf.FilterMode           = CAN_FILTERMODE_IDMASK;
    sf.FilterScale          = CAN_FILTERSCALE_32BIT;
    sf.FilterFIFOAssignment = CAN_RX_FIFO0;
    sf.FilterActivation     = ENABLE;
    sf.SlaveStartFilterBank = 14;
    HAL_CAN_ConfigFilter(&hcan, &sf);
    HAL_CAN_Start(&hcan);

    txHeader.StdId              = 0x123;
    txHeader.IDE                = CAN_ID_STD;
    txHeader.RTR                = CAN_RTR_DATA;
    txHeader.DLC                = 8;
    txHeader.TransmitGlobalTime = DISABLE;
    /* USER CODE END 2 */

    /* USER CODE BEGIN WHILE */
    uint8_t seq = 0, lastTemp = 0, lastHum = 0;

    while (1)
    {
        uint16_t dist  = HCSR04_Read();
        uint16_t light = LDR_Read();

        /* DHT11 max sample rate ~1 Hz: only read on even sequence counts */
        if ((seq & 1) == 0) {
            uint8_t tC, hP;
            if (DHT11_Read(&tC, &hP)) { lastTemp = tC; lastHum = hP; }
        }

        txData[0] = dist  & 0xFF;
        txData[1] = dist  >> 8;
        txData[2] = (uint8_t)(int8_t)lastTemp;
        txData[3] = lastHum;
        txData[4] = light & 0xFF;
        txData[5] = light >> 8;
        txData[6] = (uint8_t)((light * 100UL) / 4095UL);
        txData[7] = seq++;

        if (HAL_CAN_GetTxMailboxesFreeLevel(&hcan) > 0)
        {
            HAL_CAN_AddTxMessage(&hcan, &txHeader, txData, &txMailbox);
            HAL_GPIO_TogglePin(GPIOC, GPIO_PIN_13);   /* heartbeat */
        }

        HAL_Delay(500);
        /* USER CODE END WHILE */

        /* USER CODE BEGIN 3 */
    }
    /* USER CODE END 3 */
}


/* ============================================================
   SystemClock_Config — HSE 8 MHz x9 PLL = 72 MHz SYSCLK
   APB1 = 36 MHz (required for CAN 500 kbps, prescaler=6 BS1=8 BS2=3)
   APB2 = 72 MHz
   ============================================================ */
void SystemClock_Config(void)
{
    RCC_OscInitTypeDef RCC_OscInitStruct = {0};
    RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

    RCC_OscInitStruct.OscillatorType      = RCC_OSCILLATORTYPE_HSE;
    RCC_OscInitStruct.HSEState            = RCC_HSE_ON;
    RCC_OscInitStruct.HSEPredivValue      = RCC_HSE_PREDIV_DIV1;
    RCC_OscInitStruct.HSIState            = RCC_HSI_ON;
    RCC_OscInitStruct.PLL.PLLState        = RCC_PLL_ON;
    RCC_OscInitStruct.PLL.PLLSource       = RCC_PLLSOURCE_HSE;
    RCC_OscInitStruct.PLL.PLLMUL          = RCC_PLL_MUL9;  /* 8 MHz x9 = 72 MHz */
    if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK) {
        Error_Handler();
    }

    RCC_ClkInitStruct.ClockType      = RCC_CLOCKTYPE_HCLK  | RCC_CLOCKTYPE_SYSCLK
                                     | RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2;
    RCC_ClkInitStruct.SYSCLKSource   = RCC_SYSCLKSOURCE_PLLCLK;
    RCC_ClkInitStruct.AHBCLKDivider  = RCC_SYSCLK_DIV1;
    RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV2;   /* APB1 = 36 MHz */
    RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;   /* APB2 = 72 MHz */

    if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_2) != HAL_OK) {
        Error_Handler();
    }
}


/* ============================================================
   MX_GPIO_Init
   ============================================================ */
static void MX_GPIO_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStruct = {0};

    __HAL_RCC_GPIOC_CLK_ENABLE();
    __HAL_RCC_GPIOD_CLK_ENABLE();
    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_GPIOB_CLK_ENABLE();

    /* PC13 — heartbeat LED (active low on Blue Pill) */
    HAL_GPIO_WritePin(GPIOC, GPIO_PIN_13, GPIO_PIN_SET);
    GPIO_InitStruct.Pin   = GPIO_PIN_13;
    GPIO_InitStruct.Mode  = GPIO_MODE_OUTPUT_PP;
    GPIO_InitStruct.Pull  = GPIO_NOPULL;
    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
    HAL_GPIO_Init(GPIOC, &GPIO_InitStruct);

    /* PB8 — HC-SR04 TRIG output */
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8, GPIO_PIN_RESET);
    GPIO_InitStruct.Pin   = GPIO_PIN_8;
    GPIO_InitStruct.Mode  = GPIO_MODE_OUTPUT_PP;
    GPIO_InitStruct.Pull  = GPIO_NOPULL;
    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_HIGH;
    HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);

    /* PB9 — HC-SR04 ECHO input */
    GPIO_InitStruct.Pin  = GPIO_PIN_9;
    GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
    GPIO_InitStruct.Pull = GPIO_NOPULL;
    HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);

    /* PB6 — DHT11 DATA; DHT_SetOutput/Input reconfigure at runtime */
    GPIO_InitStruct.Pin  = GPIO_PIN_6;
    GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
    GPIO_InitStruct.Pull = GPIO_PULLUP;
    HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);

    /* PA0 — configured as analog by MX_ADC1_Init / HAL_ADC_MspInit  */
    /* PA11/PA12 — configured as CAN AF by MX_CAN_Init / HAL_CAN_MspInit */
}


/* ============================================================
   MX_CAN_Init — 500 kbps on APB1=36 MHz
   36 MHz / (6 * (1+8+3)) = 500 kbps, sample point 75 %
   ============================================================ */
static void MX_CAN_Init(void)
{
    hcan.Instance                  = CAN1;
    hcan.Init.Prescaler            = 6;
    hcan.Init.Mode                 = CAN_MODE_NORMAL;
    hcan.Init.SyncJumpWidth        = CAN_SJW_1TQ;
    hcan.Init.TimeSeg1             = CAN_BS1_8TQ;
    hcan.Init.TimeSeg2             = CAN_BS2_3TQ;
    hcan.Init.TimeTriggeredMode    = DISABLE;
    hcan.Init.AutoBusOff           = DISABLE;
    hcan.Init.AutoWakeUp           = DISABLE;
    hcan.Init.AutoRetransmission   = ENABLE;
    hcan.Init.ReceiveFifoLocked    = DISABLE;
    hcan.Init.TransmitFifoPriority = DISABLE;
    if (HAL_CAN_Init(&hcan) != HAL_OK) {
        Error_Handler();
    }
}


/* ============================================================
   MX_TIM1_Init — 1 us/tick microsecond timebase
   72 MHz / (71+1) = 1 MHz = 1 us per tick
   ============================================================ */
static void MX_TIM1_Init(void)
{
    TIM_ClockConfigTypeDef  sClockSourceConfig = {0};
    TIM_MasterConfigTypeDef sMasterConfig      = {0};

    htim1.Instance               = TIM1;
    htim1.Init.Prescaler         = 71;
    htim1.Init.CounterMode       = TIM_COUNTERMODE_UP;
    htim1.Init.Period            = 65535;
    htim1.Init.ClockDivision     = TIM_CLOCKDIVISION_DIV1;
    htim1.Init.RepetitionCounter = 0;
    htim1.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_DISABLE;
    if (HAL_TIM_Base_Init(&htim1) != HAL_OK) {
        Error_Handler();
    }

    sClockSourceConfig.ClockSource = TIM_CLOCKSOURCE_INTERNAL;
    if (HAL_TIM_ConfigClockSource(&htim1, &sClockSourceConfig) != HAL_OK) {
        Error_Handler();
    }

    sMasterConfig.MasterOutputTrigger = TIM_TRGO_RESET;
    sMasterConfig.MasterSlaveMode     = TIM_MASTERSLAVEMODE_DISABLE;
    if (HAL_TIMEx_MasterConfigSynchronization(&htim1, &sMasterConfig) != HAL_OK) {
        Error_Handler();
    }
}


/* ============================================================
   MX_ADC1_Init — single-channel polling on PA0 (IN0)
   ============================================================ */
static void MX_ADC1_Init(void)
{
    ADC_ChannelConfTypeDef sConfig = {0};

    hadc1.Instance                   = ADC1;
    hadc1.Init.ScanConvMode          = ADC_SCAN_DISABLE;
    hadc1.Init.ContinuousConvMode    = DISABLE;
    hadc1.Init.DiscontinuousConvMode = DISABLE;
    hadc1.Init.ExternalTrigConv      = ADC_SOFTWARE_START;
    hadc1.Init.DataAlign             = ADC_DATAALIGN_RIGHT;
    hadc1.Init.NbrOfConversion       = 1;
    if (HAL_ADC_Init(&hadc1) != HAL_OK) {
        Error_Handler();
    }

    sConfig.Channel      = ADC_CHANNEL_0;
    sConfig.Rank         = ADC_REGULAR_RANK_1;
    sConfig.SamplingTime = ADC_SAMPLETIME_239CYCLES_5;   /* long sample for high-Z divider */
    if (HAL_ADC_ConfigChannel(&hadc1, &sConfig) != HAL_OK) {
        Error_Handler();
    }
}


/* ============================================================
   MSP (low-level peripheral clock + GPIO init)
   These override the __weak stubs in the HAL library.
   If your stm32f1xx_hal_msp.c already has these functions,
   move the bodies there and delete them from here.
   ============================================================ */

/* CAN1: enable clock, configure PA11 (RX input) and PA12 (TX AF push-pull) */
void HAL_CAN_MspInit(CAN_HandleTypeDef *hcan_in)
{
    GPIO_InitTypeDef GPIO_InitStruct = {0};
    if (hcan_in->Instance == CAN1)
    {
        __HAL_RCC_CAN1_CLK_ENABLE();
        __HAL_RCC_GPIOA_CLK_ENABLE();

        GPIO_InitStruct.Pin  = GPIO_PIN_11;          /* CAN_RX */
        GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
        GPIO_InitStruct.Pull = GPIO_NOPULL;
        HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

        GPIO_InitStruct.Pin   = GPIO_PIN_12;         /* CAN_TX */
        GPIO_InitStruct.Mode  = GPIO_MODE_AF_PP;
        GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_HIGH;
        HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);
    }
}

/* TIM1: enable clock */
void HAL_TIM_Base_MspInit(TIM_HandleTypeDef *htim_base)
{
    if (htim_base->Instance == TIM1)
    {
        __HAL_RCC_TIM1_CLK_ENABLE();
    }
}

/* ADC1: enable clock, configure PA0 as analog */
void HAL_ADC_MspInit(ADC_HandleTypeDef *hadc_in)
{
    GPIO_InitTypeDef GPIO_InitStruct = {0};
    if (hadc_in->Instance == ADC1)
    {
        __HAL_RCC_ADC1_CLK_ENABLE();
        __HAL_RCC_GPIOA_CLK_ENABLE();

        GPIO_InitStruct.Pin  = GPIO_PIN_0;           /* PA0 = ADC1_IN0 */
        GPIO_InitStruct.Mode = GPIO_MODE_ANALOG;
        HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);
    }
}


/* USER CODE BEGIN 4 */
/* USER CODE END 4 */

void Error_Handler(void)
{
    /* USER CODE BEGIN Error_Handler_Debug */
    __disable_irq();
    while (1) {}
    /* USER CODE END Error_Handler_Debug */
}

#ifdef USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line)
{
    /* USER CODE BEGIN 6 */
    /* USER CODE END 6 */
}
#endif

/************************ (C) COPYRIGHT STMicroelectronics *****END OF FILE****/
